"""Crypto adapter + generic-guard integration tests (CCXT mocked — fully offline)."""

from decimal import Decimal

import pytest

from crypto_agent.adapters.ccxt import CcxtBroker, CcxtClient, CcxtMarketData, CryptoExchangeError
from trading_core.domain.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from trading_core.journal import TradeJournal
from trading_core.safety import GuardedBroker, SafetyError

_MARKETS = {
    "BTC/USDT": {
        "base": "BTC",
        "quote": "USDT",
        "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}},
    }
}


class FakeExchange:
    """Minimal CCXT-async-shaped double recording the calls the adapter makes."""

    def __init__(self, *, balance=None, last=100.0, has_cost=True):
        self.id = "fakex"
        self.markets = _MARKETS
        self.has = {"createMarketBuyOrderWithCost": has_cost, "createStopLimitOrder": True}
        # Faithful to ccxt's binance defaults: a symbol-less fetch_open_orders RAISES unless
        # this option is acknowledged off (an infidelity here hid a real production bug).
        self.options = {"warnOnFetchOpenOrdersWithoutSymbol": True}
        self._balance = balance or {"free": {"USDT": 1000.0}, "total": {"USDT": 1000.0}}
        self._last = last
        self.created: list[tuple] = []
        self.created_params: list[dict] = []
        self.cost_orders: list[tuple] = []
        self.cancelled: list[tuple] = []
        self.open_orders: list[dict] = [
            {"id": "open-1", "symbol": "BTC/USDT", "side": "sell", "status": "open"}
        ]

    def set_sandbox_mode(self, _flag):  # pragma: no cover - not hit (exchange injected)
        ...

    def amount_to_precision(self, _symbol, amount):
        return format(round(float(amount), 6), "f")

    def price_to_precision(self, _symbol, price):
        return format(float(price), "f")

    async def load_markets(self):
        return self.markets

    async def fetch_balance(self):
        return self._balance

    async def fetch_ticker(self, _symbol):
        return {"last": self._last, "bid": self._last - 1, "ask": self._last + 1}

    async def fetch_tickers(self, symbols):
        return {s: await self.fetch_ticker(s) for s in symbols}

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.created.append((symbol, type_, side, amount, price))
        self.created_params.append(params or {})
        return {
            "id": "o1", "symbol": symbol, "side": side, "status": "closed",
            # CCXT always normalizes `filled` to BASE units and reports the quote spent in
            # `cost` — unlike IBKR, whose cash orders report the fill in dollars (ADR-017).
            "filled": amount, "average": self._last, "amount": amount, "price": price,
            "cost": amount * self._last,
        }

    async def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        self.cost_orders.append((symbol, cost))
        return {
            "id": "c1", "symbol": symbol, "side": "buy", "status": "closed",
            "filled": cost / self._last, "average": self._last, "cost": cost,
        }

    async def fetch_order(self, order_id, symbol):
        return {"id": order_id, "symbol": symbol, "side": "buy", "status": "open"}

    async def cancel_order(self, order_id, symbol):
        self.cancelled.append((order_id, symbol))
        return {"id": order_id, "symbol": symbol, "side": "buy", "status": "canceled"}

    async def fetch_open_orders(self, symbol=None):
        # Mirrors ccxt 4.5.x binance behavior EXACTLY: without a symbol and with the default
        # warn option on, it raises ExchangeError instead of returning the orders.
        if symbol is None and self.options.get("warnOnFetchOpenOrdersWithoutSymbol", True):
            from ccxt.base.errors import ExchangeError

            raise ExchangeError(
                self.id + " fetchOpenOrders() WARNING: fetching open orders without "
                "specifying a symbol has stricter rate limits. To acknowledge this warning, "
                'set fakex.options["warnOnFetchOpenOrdersWithoutSymbol"] = False.'
            )
        return list(self.open_orders)

    async def close(self):  # pragma: no cover
        ...


def _client(ex: FakeExchange) -> CcxtClient:
    return CcxtClient("binance", sandbox=True, quote_currency="USDT", exchange=ex)


def _broker(ex: FakeExchange) -> CcxtBroker:
    return CcxtBroker(_client(ex))


# --- symbols & decimals -------------------------------------------------------------

def test_normalize_symbol_defaults_quote():
    client = _client(FakeExchange())
    assert client.normalize_symbol("btc") == "BTC/USDT"
    assert client.normalize_symbol("eth/usdt") == "ETH/USDT"


# --- buys ---------------------------------------------------------------------------

async def test_buy_by_cost_uses_native_with_cost():
    ex = FakeExchange(has_cost=True)
    broker = _broker(ex)
    result = await broker.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    assert ex.cost_orders == [("BTC/USDT", 50.0)]
    assert ex.created == []  # native path, not the fallback create_order
    assert result.status is OrderStatus.FILLED
    # Same unit contract as IBKR (ADR-017): quantity in BASE units, cash in the quote ccy.
    # CCXT already reports `filled` in base, so there is nothing to undo here — but the
    # contract is asserted on BOTH venues so it can't silently rot on either.
    assert result.filled_quantity == Decimal("0.5")  # 50 USDT / 100 = 0.5 BTC
    assert result.filled_cash == Decimal("50")
    assert result.is_cash_quantity is True
    assert result.quantity_is_estimate is False


async def test_buy_by_cost_falls_back_without_native_support():
    ex = FakeExchange(has_cost=False, last=100.0)
    broker = _broker(ex)
    await broker.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    # 50 / 100 = 0.5 BTC via a market buy by amount
    assert ex.cost_orders == []
    assert ex.created == [("BTC/USDT", "market", "buy", 0.5, None)]


async def test_buy_by_quantity_market():
    ex = FakeExchange()
    broker = _broker(ex)
    await broker.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.25"))
    )
    assert ex.created == [("BTC/USDT", "market", "buy", 0.25, None)]


async def test_sell_by_quantity_limit():
    ex = FakeExchange()
    broker = _broker(ex)
    await broker.place_order(
        OrderRequest(
            symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT, limit_price=Decimal("105"),
        )
    )
    assert ex.created == [("BTC/USDT", "limit", "sell", 0.1, 105.0)]


# --- native stops -------------------------------------------------------------------

async def test_stop_limit_sell_places_native_trigger_order():
    ex = FakeExchange()
    broker = _broker(ex)
    result = await broker.place_order(
        OrderRequest(
            symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"),
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("96"), limit_price=Decimal("95"),
        )
    )
    assert ex.created == [("BTC/USDT", "limit", "sell", 0.1, 95.0)]
    assert ex.created_params[-1]["triggerPrice"] == 96.0
    assert result.status is OrderStatus.FILLED  # fake echoes 'closed'; shape is what matters


async def test_stop_market_refused_when_exchange_lacks_it():
    # binance spot: createStopMarketOrder is False — a bare STOP (market on trigger) must be
    # refused with guidance to pass limit_price, never silently converted.
    ex = FakeExchange()
    broker = _broker(ex)
    with pytest.raises(CryptoExchangeError, match="limit_price"):
        await broker.place_order(
            OrderRequest(
                symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"),
                order_type=OrderType.STOP, stop_price=Decimal("96"),
            )
        )
    assert ex.created == []


async def test_stop_market_allowed_when_exchange_supports_it():
    ex = FakeExchange()
    ex.has["createStopMarketOrder"] = True
    broker = _broker(ex)
    await broker.place_order(
        OrderRequest(
            symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"),
            order_type=OrderType.STOP, stop_price=Decimal("96"),
        )
    )
    assert ex.created == [("BTC/USDT", "market", "sell", 0.1, None)]
    assert ex.created_params[-1]["triggerPrice"] == 96.0


async def test_stop_limit_refused_when_exchange_has_no_stop_support():
    ex = FakeExchange()
    ex.has["createStopLimitOrder"] = False
    broker = _broker(ex)
    with pytest.raises(CryptoExchangeError, match="soft stop"):
        await broker.place_order(
            OrderRequest(
                symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"),
                order_type=OrderType.STOP_LIMIT,
                stop_price=Decimal("96"), limit_price=Decimal("95"),
            )
        )
    assert ex.created == []


async def test_buy_below_min_notional_is_blocked():
    broker = _broker(FakeExchange())
    with pytest.raises(CryptoExchangeError, match="minimum notional"):
        await broker.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("2"))
        )


async def test_buy_quantity_below_min_amount_is_blocked():
    broker = _broker(FakeExchange())
    with pytest.raises(CryptoExchangeError, match="below the market minimum"):
        await broker.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.00001"))
        )


# --- reads --------------------------------------------------------------------------

async def test_get_positions_excludes_quote_currency():
    ex = FakeExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    md = CcxtMarketData(_client(ex))
    rows = await md.get_positions()
    assert [(p.symbol, p.quantity) for p in rows] == [("BTC", Decimal("0.5"))]


async def test_held_quantity_returns_base_total():
    ex = FakeExchange(
        balance={"free": {"BTC": 0.4}, "total": {"BTC": 0.5}}
    )
    md = CcxtMarketData(_client(ex))
    assert await md.held_quantity("BTC") == Decimal("0.5")


async def test_account_summary_reports_free_quote():
    md = CcxtMarketData(_client(FakeExchange()))
    summary = await md.get_account_summary()
    assert summary.currency == "USDT"
    assert summary.available_funds == Decimal("1000")


async def test_cancel_uses_symbol_cache_from_open_orders():
    ex = FakeExchange()
    broker = _broker(ex)
    result = await broker.cancel_order("open-1")  # symbol resolved via fetch_open_orders
    assert ex.cancelled == [("open-1", "BTC/USDT")]
    assert result.status is OrderStatus.CANCELLED


# --- generic guard wired for the crypto venue ---------------------------------------

def _sandbox_provider(is_paper=True):
    async def provider():
        return {"account_id": "binance", "is_paper": is_paper, "account_type":
                "PAPER" if is_paper else "LIVE"}
    return provider


def _guarded(ex: FakeExchange, **kw):
    client = _client(ex)
    md = CcxtMarketData(client)
    broker = CcxtBroker(client)
    defaults = dict(
        mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("100"), require_market_open=False,
        is_market_open=lambda: True, allow_short=False,
        account_info_provider=_sandbox_provider(), configured_account_id="",
        venue="the binance exchange", live_env_var="CRYPTO_ALLOW_LIVE",
        mode_env_var="CRYPTO_TRADING_MODE", account_env_var="CRYPTO_API_KEY",
    )
    defaults.update(kw)
    return GuardedBroker(broker, md, **defaults)


async def test_guard_spot_blocks_naked_short():
    # No BTC held → selling any opens a short, blocked because allow_short is off (spot-only).
    ex = FakeExchange(balance={"free": {}, "total": {}})
    guarded = _guarded(ex)
    with pytest.raises(SafetyError, match="open a short"):
        await guarded.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.SELL, quantity=Decimal("0.1"))
        )


async def test_guard_cash_buy_over_max_order_value_blocked():
    guarded = _guarded(FakeExchange())
    with pytest.raises(SafetyError, match="exceeds"):
        await guarded.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("500"))
        )


async def test_guard_live_blocked_without_crypto_allow_live():
    guarded = _guarded(
        FakeExchange(), mode=TradingMode.LIVE,
        account_info_provider=_sandbox_provider(is_paper=False),
    )
    with pytest.raises(SafetyError, match="CRYPTO_ALLOW_LIVE"):
        await guarded.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("10"))
        )


async def test_guard_dry_run_does_not_send():
    ex = FakeExchange()
    guarded = _guarded(ex, dry_run=True)
    result = await guarded.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    assert result.dry_run is True
    assert ex.cost_orders == [] and ex.created == []


async def test_guard_allows_spot_buy_in_sandbox():
    ex = FakeExchange()
    guarded = _guarded(ex)
    result = await guarded.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    assert result.status is OrderStatus.FILLED
    assert ex.cost_orders == [("BTC/USDT", 50.0)]


def _unverified_provider(is_paper=True):
    """Mirrors the REAL crypto probe: PAPER is derived from config, not venue-verified."""
    async def provider():
        return {"account_id": "binance", "is_paper": is_paper,
                "account_type": "PAPER" if is_paper else "LIVE", "identity_verified": False}
    return provider


async def test_guard_unverified_identity_requires_ack_for_real_order():
    # The Task 3 mismatch path: a sandbox label whose paper/live status the venue can't
    # confirm. A real (non-dry-run) order must require CRYPTO_ALLOW_LIVE — so live keys hidden
    # under a sandbox label can't move money on the dry-run flag alone.
    guarded = _guarded(FakeExchange(), account_info_provider=_unverified_provider(),
                       dry_run=False, allow_live=False)
    with pytest.raises(SafetyError, match="CRYPTO_ALLOW_LIVE"):
        await guarded.place_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
        )


async def test_guard_unverified_identity_dry_run_still_allowed():
    # Dry-run validates freely even with unverified identity — no money can move.
    guarded = _guarded(FakeExchange(), account_info_provider=_unverified_provider(),
                       dry_run=True, allow_live=False)
    result = await guarded.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    assert result.dry_run is True


async def test_guard_unverified_identity_real_order_ok_with_ack():
    ex = FakeExchange()
    guarded = _guarded(ex, account_info_provider=_unverified_provider(),
                       dry_run=False, allow_live=True)
    result = await guarded.place_order(
        OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )
    assert result.status is OrderStatus.FILLED


class _FakeCryptoServices:
    """Stand-in for the crypto Services that session_status reads (offline)."""

    def __init__(self, settings, *, is_paper=False, journal=None):
        from trading_core.journal import TradeJournal

        self.settings = settings
        self._is_paper = is_paper
        # An empty/unwritten journal: read() on a missing file returns [] (no spend, none
        # unresolved), so the posture fields compute without touching disk.
        self.journal = journal or TradeJournal("logs/does-not-exist.jsonl")

    async def account_info(self) -> dict:
        return {
            "account_id": "binance",
            "is_paper": self._is_paper,
            "account_type": "PAPER" if self._is_paper else "LIVE",
            "identity_verified": False,
        }


async def test_crypto_session_status_warns_when_live_and_no_daily_cap(monkeypatch):
    from crypto_agent.config import CryptoMode, CryptoSettings
    from crypto_agent.server import app as crypto_app

    settings = CryptoSettings(
        max_daily_value=None, crypto_allow_live=True, crypto_trading_mode=CryptoMode.LIVE
    )
    monkeypatch.setattr(crypto_app, "_services", _FakeCryptoServices(settings))
    out = await crypto_app.session_status()
    assert out["ok"] is True
    assert out["data"]["daily_cap_configured"] is False
    assert "MAX_DAILY_VALUE" in out["data"]["daily_cap_warning"]


async def test_crypto_session_status_silent_when_cap_set(monkeypatch):
    from crypto_agent.config import CryptoMode, CryptoSettings
    from crypto_agent.server import app as crypto_app

    settings = CryptoSettings(
        max_daily_value=Decimal("500"),
        crypto_allow_live=True,
        crypto_trading_mode=CryptoMode.LIVE,
    )
    monkeypatch.setattr(crypto_app, "_services", _FakeCryptoServices(settings))
    out = await crypto_app.session_status()
    assert out["data"]["daily_cap_configured"] is True
    assert "daily_cap_warning" not in out["data"]


async def test_crypto_session_status_silent_when_live_off(monkeypatch):
    from crypto_agent.config import CryptoMode, CryptoSettings
    from crypto_agent.server import app as crypto_app

    settings = CryptoSettings(
        max_daily_value=None, crypto_allow_live=False, crypto_trading_mode=CryptoMode.SANDBOX
    )
    monkeypatch.setattr(crypto_app, "_services", _FakeCryptoServices(settings, is_paper=True))
    out = await crypto_app.session_status()
    assert out["data"]["daily_cap_configured"] is False
    assert "daily_cap_warning" not in out["data"]


# --- V1 regression: binance's fetch_open_orders warn-raise (defaults) ------------------

async def test_fake_exchange_mirrors_binance_symbolless_fetch_raise():
    # The fake must reproduce binance's real behavior: with the stock options, a
    # symbol-less fetch_open_orders RAISES. An unfaithful fake hid this bug from the suite.
    from ccxt.base.errors import ExchangeError

    ex = FakeExchange()
    with pytest.raises(ExchangeError, match="warnOnFetchOpenOrdersWithoutSymbol"):
        await ex.fetch_open_orders()


def test_client_acknowledges_the_fetch_open_orders_warning():
    ex = FakeExchange()
    _client(ex)
    assert ex.options["warnOnFetchOpenOrdersWithoutSymbol"] is False


async def test_get_live_orders_survives_binance_default_options():
    # REGRESSION (V1): get_live_orders calls fetch_open_orders with NO symbol; on binance
    # (the default exchange) that raises unless the client acknowledged the warn option —
    # which killed open_orders, reconcile_pending and post-restart stop cancels.
    ex = FakeExchange()
    broker = _broker(ex)
    rows = await broker.get_live_orders()
    assert [r.order_id for r in rows] == ["open-1"]


# --- V4: order id → symbol resolution beyond the session cache -------------------------

def _journal_with_order(tmp_path, *, order_id="hist-9", coid=None) -> TradeJournal:
    journal = TradeJournal(tmp_path / "crypto_trades.jsonl", market_timezone="UTC")
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.1"))
    placed = OrderResult(order_id=order_id, status=OrderStatus.FILLED, symbol="BTC/USDT",
                         side=OrderSide.BUY)
    journal.record(request=request, mode=TradingMode.PAPER, dry_run=False,
                   notional=Decimal("10"), result=placed, sent=True, client_order_id=coid)
    return journal


async def test_order_status_resolves_symbol_from_journal_after_restart(tmp_path):
    # The in-memory cache dies with the process and a FILLED order is not among the open
    # orders — before the fix this was "Unknown order". The journal remembers the symbol.
    ex = FakeExchange()
    ex.open_orders = []
    broker = CcxtBroker(_client(ex), journal=_journal_with_order(tmp_path))
    result = await broker.get_order_status("hist-9")
    assert result.symbol == "BTC/USDT"
    assert result.status is OrderStatus.SUBMITTED  # the fake echoes an "open" order


async def test_cancel_resolves_symbol_from_journal_by_client_order_id(tmp_path):
    ex = FakeExchange()
    ex.open_orders = []
    journal = _journal_with_order(tmp_path, order_id=None, coid="coid-7")
    broker = CcxtBroker(_client(ex), journal=journal)
    await broker.cancel_order("coid-7")
    assert ex.cancelled == [("coid-7", "BTC/USDT")]


async def test_order_status_with_explicit_symbol_bypasses_resolution():
    ex = FakeExchange()
    ex.open_orders = []  # nothing open, no journal — only the explicit symbol can work
    broker = _broker(ex)
    result = await broker.get_order_status("ext-1", symbol="btc")
    assert result.symbol == "BTC/USDT"


async def test_cancel_with_explicit_symbol_bypasses_resolution():
    ex = FakeExchange()
    ex.open_orders = []
    broker = _broker(ex)
    result = await broker.cancel_order("ext-2", symbol="BTC")
    assert ex.cancelled == [("ext-2", "BTC/USDT")]
    assert result.status is OrderStatus.CANCELLED


async def test_unknown_order_error_suggests_passing_the_symbol():
    ex = FakeExchange()
    ex.open_orders = []
    broker = _broker(ex)
    with pytest.raises(CryptoExchangeError, match="symbol"):
        await broker.get_order_status("nope")


async def test_guarded_broker_forwards_symbol_to_crypto_cancel():
    ex = FakeExchange()
    ex.open_orders = []
    guarded = _guarded(ex)
    await guarded.cancel_order("ext-3", symbol="BTC")
    assert ex.cancelled == [("ext-3", "BTC/USDT")]


# --- V2: close_position vs balance locked by a resting stop ----------------------------

class _StopLockedExchange(FakeExchange):
    """0.5 BTC held; part/all of it locked by a resting stop-limit SELL (binance semantics:
    an untriggered stop moves the base out of ``free`` while it stays in ``total``)."""

    def __init__(self, *, free="0", cancel_fails=False):
        super().__init__(
            balance={
                "free": {"BTC": float(free), "USDT": 1000.0},
                "total": {"BTC": 0.5, "USDT": 1000.0},
            }
        )
        self.open_orders = [
            {"id": "stop-1", "symbol": "BTC/USDT", "side": "sell", "status": "open"}
        ]
        self._cancel_fails = cancel_fails

    async def cancel_order(self, order_id, symbol):
        if self._cancel_fails:
            raise RuntimeError("cancel rejected by the exchange")
        self.cancelled.append((order_id, symbol))
        self.open_orders = []
        self._balance["free"]["BTC"] = self._balance["total"]["BTC"]  # the lock is released
        return {"id": order_id, "symbol": symbol, "side": "sell", "status": "canceled"}

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        # Faithful to the exchange: a SELL above the FREE balance is rejected server-side
        # (the locked part backs the resting stop) — exactly the failure the old
        # close_position hit when it sold the total against a locked balance.
        if side == "sell" and amount > self._balance["free"].get("BTC", 0.0):
            from ccxt.base.errors import InsufficientFunds

            raise InsufficientFunds(
                self.id + " Account has insufficient balance for requested action."
            )
        return await super().create_order(symbol, type_, side, amount, price, params)


def _crypto_app_services(ex, tmp_path):
    from crypto_agent.config import CryptoSettings
    from crypto_agent.server.services import Services
    from trading_core.capabilities import crypto_capabilities

    client = _client(ex)
    market_data = CcxtMarketData(client)
    journal = TradeJournal(tmp_path / "crypto_trades.jsonl", market_timezone="UTC")
    broker = GuardedBroker(
        CcxtBroker(client, journal=journal), market_data,
        mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("100"), require_market_open=False,
        journal=journal, account_info_provider=_sandbox_provider(),
        venue="the binance exchange", live_env_var="CRYPTO_ALLOW_LIVE",
        mode_env_var="CRYPTO_TRADING_MODE", account_env_var="CRYPTO_API_KEY",
    )
    return Services(
        settings=CryptoSettings(), client=client, market_data=market_data, broker=broker,
        journal=journal, capabilities=crypto_capabilities("USDT", allow_margin=False),
    )


async def test_crypto_close_with_unlocked_balance_cancels_nothing(tmp_path, monkeypatch):
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()
    ex = FakeExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    out = await crypto_app.close_position("BTC")
    assert out["ok"] is True
    assert "cancelled_orders" not in out["data"]  # free == total → nothing to release
    assert ex.cancelled == []
    assert ex.created == [("BTC/USDT", "market", "sell", 0.5, None)]
    crypto_app._recent_closes.clear()


async def test_crypto_close_cancels_resting_stop_then_sells_total(tmp_path, monkeypatch):
    # REGRESSION (V2): with 0.5 BTC all locked by a resting stop, the old close sold 0.5
    # against free=0 — "insufficient balance". Now the stop is cancelled first (a closed
    # position would orphan it anyway), then the FULL balance is sold.
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()
    ex = _StopLockedExchange(free="0")
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    out = await crypto_app.close_position("BTC")
    assert out["ok"] is True
    assert ex.cancelled == [("stop-1", "BTC/USDT")]
    assert out["data"]["cancelled_orders"] == [{"order_id": "stop-1", "status": "cancelled"}]
    assert ex.created == [("BTC/USDT", "market", "sell", 0.5, None)]  # sells the TOTAL
    crypto_app._recent_closes.clear()


async def test_crypto_close_cancel_failure_sells_free_and_reports_locked(tmp_path, monkeypatch):
    # Fail-closed: if the stop can't be cancelled, sell only what is free and say exactly
    # what stayed locked and what to do about it — never fire a sell that must bounce.
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()
    ex = _StopLockedExchange(free="0.2", cancel_fails=True)
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    out = await crypto_app.close_position("BTC")
    assert out["ok"] is True
    data = out["data"]
    assert data["cancel_failures"][0]["order_id"] == "stop-1"
    assert data["locked_quantity"] == "0.3"
    assert "close_position again" in data["warning"]
    assert ex.created == [("BTC/USDT", "market", "sell", 0.2, None)]  # only the free part
    crypto_app._recent_closes.clear()


async def test_crypto_close_fully_locked_and_cancel_fails_sells_nothing(tmp_path, monkeypatch):
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()
    ex = _StopLockedExchange(free="0", cancel_fails=True)
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    out = await crypto_app.close_position("BTC")
    assert out["ok"] is True
    assert out["data"]["closed"] is False
    assert "locked" in out["data"]["reason"]
    assert ex.created == []
    # Nothing was dispatched, so the cooldown must NOT trap a retry after the fix.
    assert "BTC/USDT" not in crypto_app._recent_closes
    crypto_app._recent_closes.clear()


# --- V6: the crypto close cooldown (mirrors the IBKR coverage in test_server.py) -------

async def test_crypto_close_cooldown_blocks_immediate_reclose(tmp_path, monkeypatch):
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()
    ex = FakeExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    first = await crypto_app.close_position("BTC")
    assert first["ok"] is True
    assert first["data"].get("closed") is not False  # it actually dispatched a sell

    second = await crypto_app.close_position("BTC")
    assert second["ok"] is True
    assert second["data"]["closed"] is False
    assert "twice" in second["data"]["reason"]
    assert len(ex.created) == 1  # only ONE sell went out
    crypto_app._recent_closes.clear()


async def test_crypto_close_concurrent_double_is_prevented(tmp_path, monkeypatch):
    import asyncio

    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()

    class _SlowBalanceExchange(FakeExchange):
        async def fetch_balance(self):
            await asyncio.sleep(0.01)  # let the second call interleave after the reservation
            return self._balance

    ex = _SlowBalanceExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    results = await asyncio.gather(
        crypto_app.close_position("BTC"), crypto_app.close_position("BTC")
    )
    backed_off = [r for r in results if r["data"].get("closed") is False]
    assert len(backed_off) == 1  # one reserved, the other saw it and backed off
    assert len(ex.created) == 1  # only ONE sell dispatched (no double-close)
    crypto_app._recent_closes.clear()


async def test_crypto_close_holds_reservation_on_indeterminate(tmp_path, monkeypatch):
    # If the dispatch RAISES (an indeterminate error — the sell may have landed), the
    # reservation must be HELD so a blind retry can't sell the same balance twice.
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()

    class _RaisingExchange(FakeExchange):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.attempts = 0

        async def create_order(self, *args, **kwargs):
            self.attempts += 1
            raise RuntimeError("UNKNOWN whether the order landed")

    ex = _RaisingExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    first = await crypto_app.close_position("BTC")
    assert first["ok"] is False  # the indeterminate error surfaced
    second = await crypto_app.close_position("BTC")
    assert second["data"]["closed"] is False  # reservation held → retry backs off
    assert "twice" in second["data"]["reason"]
    assert ex.attempts == 1  # only the first attempt was sent
    crypto_app._recent_closes.clear()


async def test_crypto_close_releases_cooldown_on_rejected(tmp_path, monkeypatch):
    from crypto_agent.server import app as crypto_app

    crypto_app._recent_closes.clear()

    class _RejectingExchange(FakeExchange):
        async def create_order(self, symbol, type_, side, amount, price=None, params=None):
            self.created.append((symbol, type_, side, amount, price))
            return {"id": None, "symbol": symbol, "side": side, "status": "rejected",
                    "filled": 0}

    ex = _RejectingExchange(
        balance={"free": {"BTC": 0.5, "USDT": 1000.0}, "total": {"BTC": 0.5, "USDT": 1000.0}}
    )
    monkeypatch.setattr(crypto_app, "_services", _crypto_app_services(ex, tmp_path))

    first = await crypto_app.close_position("BTC")
    assert first["data"]["status"] == "rejected"
    # The reservation was released (nothing dispatched), so a retry is NOT cooldown-blocked.
    second = await crypto_app.close_position("BTC")
    assert second["data"].get("closed") is not False
    assert len(ex.created) == 2
    crypto_app._recent_closes.clear()
