"""Crypto adapter + generic-guard integration tests (CCXT mocked — fully offline)."""

from decimal import Decimal

import pytest

from crypto_agent.adapters.ccxt import CcxtBroker, CcxtClient, CcxtMarketData, CryptoExchangeError
from trading_core.domain.models import OrderRequest, OrderSide, OrderStatus, OrderType, TradingMode
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
        self.markets = _MARKETS
        self.has = {"createMarketBuyOrderWithCost": has_cost}
        self._balance = balance or {"free": {"USDT": 1000.0}, "total": {"USDT": 1000.0}}
        self._last = last
        self.created: list[tuple] = []
        self.cost_orders: list[tuple] = []
        self.cancelled: list[tuple] = []

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
        return {
            "id": "o1", "symbol": symbol, "side": side, "status": "closed",
            "filled": amount, "average": self._last, "amount": amount, "price": price,
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
        return [{"id": "open-1", "symbol": "BTC/USDT", "side": "sell", "status": "open"}]

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
