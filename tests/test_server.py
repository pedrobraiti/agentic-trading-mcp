from decimal import Decimal

import pytest

from ibkr_agent.config import Settings
from ibkr_agent.server import app
from ibkr_agent.server.app import mcp
from ibkr_agent.server.services import Services, build_services
from trading_core.domain.models import (
    AccountSummary,
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    TradingMode,
)
from trading_core.journal import TradeJournal
from trading_core.safety import GuardedBroker


async def test_tools_are_registered():
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    expected = {
        "session_status",
        "market_status",
        "get_quote",
        "get_quotes",
        "account_summary",
        "positions",
        "portfolio",
        "wait_for_fill",
        "buy",
        "sell",
        "close_position",
        "stop_order",
        "trailing_stop",
        "bracket_order",
        "preview_order",
        "order_status",
        "cancel_order",
        "open_orders",
        "trade_history",
        "reconcile_pending",
    }
    assert expected <= names


def test_build_services_wires_without_network():
    svc = build_services(Settings(ibkr_account_id="DU1", trading_dry_run=True))
    assert svc.settings.ibkr_account_id == "DU1"
    assert isinstance(svc.market_is_open(), bool)
    assert svc.broker is not None


class _FakeAuth:
    def __init__(self, *, is_paper: bool = True, authenticated: bool = True):
        self._is_paper = is_paper
        self._authenticated = authenticated

    async def ensure_session(self) -> None: ...

    async def status(self) -> dict:
        return {"authenticated": self._authenticated, "connected": True, "competing": False}

    async def account_info(self) -> dict:
        account_id = "DU1" if self._is_paper else "U1"
        return {
            "account_id": account_id,
            "is_paper": self._is_paper,
            "account_type": "PAPER" if self._is_paper else "LIVE",
        }


class _FakeMarketData:
    async def resolve_conid(self, symbol: str) -> int:
        return 1

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, conid=1, last_price=Decimal("10"))

    async def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [Quote(symbol=s.upper(), conid=1, last_price=Decimal("10")) for s in symbols]

    async def get_account_summary(self) -> AccountSummary:
        return AccountSummary(account_id="DU1", available_funds=Decimal("100"))

    async def get_positions(self) -> list[Position]:
        return [Position(conid=1, symbol="AAPL", quantity=Decimal("1"),
                         unrealized_pnl=Decimal("2"))]

    async def invalidate_positions(self) -> None: ...


class _FakeInner:
    def __init__(self):
        self.placed: list[OrderRequest] = []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.placed.append(request)
        return OrderResult(order_id="x", status=OrderStatus.SUBMITTED,
                           symbol=request.symbol, side=request.side)

    async def place_bracket(self, bracket) -> list[OrderResult]:
        self.placed.append(bracket.entry)
        return [
            OrderResult(order_id="p", status=OrderStatus.SUBMITTED,
                        symbol=bracket.entry.symbol, side=bracket.entry.side, message="entry"),
            OrderResult(order_id="tp", status=OrderStatus.SUBMITTED,
                        symbol=bracket.entry.symbol, side=OrderSide.SELL, message="take_profit"),
            OrderResult(order_id="sl", status=OrderStatus.SUBMITTED,
                        symbol=bracket.entry.symbol, side=OrderSide.SELL, message="stop_loss"),
        ]

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        return OrderPreview(symbol=request.symbol, side=request.side)

    async def get_order_status(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.FILLED,
                           symbol="AAPL", side=OrderSide.BUY,
                           filled_quantity=Decimal("0.5"), avg_price=Decimal("10"))

    async def cancel_order(self, order_id: str) -> OrderResult:
        raise NotImplementedError

    async def get_live_orders(self) -> list[OrderResult]:
        return []


async def test_smoke_buy_dry_run_and_portfolio_through_tools(tmp_path, monkeypatch):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    market_data = _FakeMarketData()
    broker = GuardedBroker(
        _FakeInner(), market_data, mode=TradingMode.PAPER, allow_live=False,
        dry_run=True, max_order_value=Decimal("1000"), require_market_open=False,
        journal=journal,
    )
    svc = Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
        market_data=market_data, broker=broker, journal=journal,
    )
    monkeypatch.setattr(app, "_services", svc)

    bought = await app.buy("AAPL", cash_amount=10)
    assert bought["ok"] is True
    assert bought["data"]["dry_run"] is True

    snapshot = await app.portfolio()
    assert snapshot["ok"] is True
    assert snapshot["data"]["unrealized_pnl"] == "2"
    assert snapshot["data"]["account_type"] == "PAPER"

    history = await app.trade_history()
    assert history["ok"] is True
    assert len(history["data"]) == 1  # the dry-run buy was journaled


async def test_order_status_tool(monkeypatch):
    svc = Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
        market_data=_FakeMarketData(),
        broker=GuardedBroker(
            _FakeInner(), _FakeMarketData(), mode=TradingMode.PAPER, allow_live=False,
            dry_run=True, max_order_value=Decimal("1000"), require_market_open=False,
        ),
        journal=TradeJournal("logs/unused.jsonl"),
    )
    monkeypatch.setattr(app, "_services", svc)

    status = await app.order_status("x")
    assert status["ok"] is True
    assert status["data"]["status"] == "filled"
    assert status["data"]["filled_quantity"] == "0.5"


async def test_limit_buy_builds_limit_order(tmp_path, monkeypatch):
    inner = _FakeInner()
    market_data = _FakeMarketData()
    broker = GuardedBroker(
        inner, market_data, mode=TradingMode.PAPER, allow_live=False,
        dry_run=False, max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
        market_data=market_data, broker=broker, journal=broker._journal,
    )
    monkeypatch.setattr(app, "_services", svc)

    ok = await app.buy("AAPL", quantity=1, limit_price=9.5)
    assert ok["ok"] is True
    assert inner.placed[-1].order_type.value == "LMT"
    assert inner.placed[-1].limit_price == Decimal("9.5")

    rejected = await app.buy("AAPL", cash_amount=10, limit_price=9.5)
    assert rejected["ok"] is False
    assert "quantity" in rejected["error"]


async def test_stop_order_builds_stop(tmp_path, monkeypatch):
    inner = _FakeInner()
    md = _FakeMarketData()
    broker = GuardedBroker(
        inner, md, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "t.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=md, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    ok = await app.stop_order("AAPL", side="SELL", quantity=1, stop_price=8.0)
    assert ok["ok"] is True
    assert inner.placed[-1].order_type.value == "STP"
    assert inner.placed[-1].stop_price == Decimal("8.0")


async def test_trailing_stop_tool(tmp_path, monkeypatch):
    inner = _FakeInner()
    md = _FakeMarketData()
    broker = GuardedBroker(
        inner, md, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "t.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=md, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    out = await app.trailing_stop("AAPL", side="SELL", quantity=1, trail_percent=5)
    assert out["ok"] is True
    assert inner.placed[-1].order_type.value == "TRAIL"
    assert inner.placed[-1].trailing_type.value == "%"

    bad = await app.trailing_stop("AAPL", side="SELL", quantity=1)  # neither amount nor pct
    assert bad["ok"] is False


async def test_bracket_order_through_tool(tmp_path, monkeypatch):
    inner = _FakeInner()
    md = _FakeMarketData()
    broker = GuardedBroker(
        inner, md, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "t.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=md, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    out = await app.bracket_order("AAPL", quantity=1, take_profit=12.0, stop_loss=8.0)
    assert out["ok"] is True
    legs = {leg["message"] for leg in out["data"]}
    assert legs == {"entry", "take_profit", "stop_loss"}


async def test_get_quotes_batch(monkeypatch):
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=_FakeMarketData(), broker=None,
                   journal=TradeJournal("logs/unused.jsonl"))
    monkeypatch.setattr(app, "_services", svc)

    out = await app.get_quotes(["aapl", "msft"])
    assert out["ok"] is True
    assert {q["symbol"] for q in out["data"]} == {"AAPL", "MSFT"}


def _services_with_auth(auth):
    return Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=auth,
        market_data=_FakeMarketData(), broker=None,
        journal=TradeJournal("logs/unused.jsonl"),
    )


async def test_session_status_flags_live_account(monkeypatch):
    monkeypatch.setattr(app, "_services", _services_with_auth(_FakeAuth(is_paper=False)))
    out = await app.session_status()
    assert out["ok"] is True
    assert out["data"]["account_type"] == "LIVE"
    assert out["data"]["is_paper"] is False
    assert "REAL money" in out["data"]["warning"]


async def test_session_status_marks_paper_without_warning(monkeypatch):
    monkeypatch.setattr(app, "_services", _services_with_auth(_FakeAuth(is_paper=True)))
    out = await app.session_status()
    assert out["ok"] is True
    assert out["data"]["account_type"] == "PAPER"
    assert "warning" not in out["data"]


async def test_session_status_skips_account_lookup_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        app, "_services", _services_with_auth(_FakeAuth(authenticated=False))
    )
    out = await app.session_status()
    assert out["ok"] is True
    assert out["data"]["authenticated"] is False
    assert "account_type" not in out["data"]


def _services_with_settings(settings):
    return Services(
        settings=settings, client=None, auth=_FakeAuth(),
        market_data=_FakeMarketData(), broker=None,
        journal=TradeJournal("logs/unused.jsonl"),
    )


async def test_session_status_warns_when_live_and_no_daily_cap(monkeypatch):
    settings = Settings(ibkr_account_id="DU1", trading_allow_live=True, max_daily_value=None)
    monkeypatch.setattr(app, "_services", _services_with_settings(settings))
    out = await app.session_status()
    assert out["data"]["daily_cap_configured"] is False
    assert "MAX_DAILY_VALUE" in out["data"]["daily_cap_warning"]


async def test_session_status_no_daily_cap_warning_when_cap_set(monkeypatch):
    settings = Settings(
        ibkr_account_id="DU1", trading_allow_live=True, max_daily_value=Decimal("500")
    )
    monkeypatch.setattr(app, "_services", _services_with_settings(settings))
    out = await app.session_status()
    assert out["data"]["daily_cap_configured"] is True
    assert "daily_cap_warning" not in out["data"]


async def test_session_status_no_daily_cap_warning_when_live_off(monkeypatch):
    settings = Settings(ibkr_account_id="DU1", trading_allow_live=False, max_daily_value=None)
    monkeypatch.setattr(app, "_services", _services_with_settings(settings))
    out = await app.session_status()
    assert out["data"]["daily_cap_configured"] is False
    assert "daily_cap_warning" not in out["data"]


async def test_session_status_reports_trading_posture(tmp_path, monkeypatch):
    settings = Settings(ibkr_account_id="DU1", trading_dry_run=True,
                        max_daily_value=Decimal("500"))
    svc = Services(
        settings=settings, client=None, auth=_FakeAuth(),
        market_data=_FakeMarketData(), broker=None,
        journal=TradeJournal(tmp_path / "t.jsonl"),
    )
    monkeypatch.setattr(app, "_services", svc)
    out = await app.session_status()
    data = out["data"]
    assert data["dry_run"] is True
    assert data["allowlist_active"] is False
    assert data["remaining_daily_budget"] == "500"
    assert data["unresolved_orders"] == []
    assert data["trade_stops"] == []
    assert data["safe_to_trade"] is True


async def test_session_status_competing_is_a_trade_stop(tmp_path, monkeypatch):
    class _CompetingAuth(_FakeAuth):
        async def status(self) -> dict:
            return {"authenticated": True, "connected": True, "competing": True}

    svc = Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=_CompetingAuth(),
        market_data=_FakeMarketData(), broker=None,
        journal=TradeJournal(tmp_path / "t.jsonl"),
    )
    monkeypatch.setattr(app, "_services", svc)
    out = await app.session_status()
    assert any("competing" in stop for stop in out["data"]["trade_stops"])
    assert out["data"]["safe_to_trade"] is False


async def test_session_status_unresolved_order_is_a_trade_stop(tmp_path, monkeypatch):
    journal = TradeJournal(tmp_path / "t.jsonl")
    buy = OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("10"))
    journal.record_intent(request=buy, mode=TradingMode.PAPER, notional=Decimal("10"),
                          client_order_id="coid-1")
    svc = Services(
        settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
        market_data=_FakeMarketData(), broker=None, journal=journal,
    )
    monkeypatch.setattr(app, "_services", svc)
    out = await app.session_status()
    assert len(out["data"]["unresolved_orders"]) == 1
    assert any("unconfirmed" in stop for stop in out["data"]["trade_stops"])
    assert out["data"]["safe_to_trade"] is False


async def test_close_position_cooldown_blocks_immediate_reclose(tmp_path, monkeypatch):
    app._recent_closes.clear()
    market_data = _FakeMarketData()
    broker = GuardedBroker(
        _FakeInner(), market_data, mode=TradingMode.PAPER, allow_live=False,
        dry_run=False, max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    first = await app.close_position("AAPL")
    assert first["ok"] is True
    assert first["data"].get("closed") is not False  # it actually dispatched a sell

    second = await app.close_position("AAPL")
    assert second["ok"] is True
    assert second["data"]["closed"] is False
    assert "twice" in second["data"]["reason"]
    app._recent_closes.clear()


async def test_close_position_concurrent_double_is_prevented(tmp_path, monkeypatch):
    import asyncio

    app._recent_closes.clear()

    class _SlowMarketData(_FakeMarketData):
        async def get_positions(self):
            await asyncio.sleep(0.01)  # let the second call interleave after the reservation
            return [Position(conid=1, symbol="AAPL", quantity=Decimal("5"))]

    market_data = _SlowMarketData()
    inner = _FakeInner()
    broker = GuardedBroker(
        inner, market_data, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    results = await asyncio.gather(app.close_position("AAPL"), app.close_position("AAPL"))
    backed_off = [r for r in results if r["data"].get("closed") is False]
    assert len(backed_off) == 1            # one reserved, the other saw it and backed off
    assert len(inner.placed) == 1          # only ONE sell dispatched (no double-close)
    app._recent_closes.clear()


async def test_close_position_holds_reservation_on_indeterminate(tmp_path, monkeypatch):
    # If place_order RAISES (e.g. an indeterminate 503 that may have landed), the
    # reservation must be HELD so a retry can't double-close — never released.
    app._recent_closes.clear()

    class _RaisingInner(_FakeInner):
        async def place_order(self, request: OrderRequest) -> OrderResult:
            self.placed.append(request)
            raise RuntimeError("UNKNOWN whether the order landed")

    market_data = _FakeMarketData()
    inner = _RaisingInner()
    broker = GuardedBroker(
        inner, market_data, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    first = await app.close_position("AAPL")
    assert first["ok"] is False  # the indeterminate error surfaced
    second = await app.close_position("AAPL")
    assert second["data"]["closed"] is False  # reservation held → retry backs off
    assert "twice" in second["data"]["reason"]
    assert len(inner.placed) == 1  # only the first attempt was sent
    app._recent_closes.clear()


async def test_close_position_cancelled_does_not_permanently_block(tmp_path, monkeypatch):
    import asyncio
    import math

    app._recent_closes.clear()

    class _CancellingInner(_FakeInner):
        async def place_order(self, request: OrderRequest) -> OrderResult:
            self.placed.append(request)
            raise asyncio.CancelledError()

    market_data = _FakeMarketData()
    broker = GuardedBroker(
        _CancellingInner(), market_data, mode=TradingMode.PAPER, allow_live=False,
        dry_run=False, max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    with pytest.raises(asyncio.CancelledError):
        await app.close_position("AAPL")

    # The in-flight sentinel was normalized to a FINITE timestamp (not left as inf forever),
    # and it evicts after the window — so the contract is not permanently blocked.
    val = app._recent_closes.get(1)
    assert val is not None and math.isfinite(val)
    app._evict_stale_closes(val + app._CLOSE_COOLDOWN_SECONDS + 1)
    assert 1 not in app._recent_closes
    app._recent_closes.clear()


async def test_close_position_inflight_reservation_not_evicted(tmp_path, monkeypatch):
    import asyncio

    app._recent_closes.clear()
    monkeypatch.setattr(app, "_CLOSE_COOLDOWN_SECONDS", 0.05)

    class _SlowInner(_FakeInner):
        async def place_order(self, request: OrderRequest) -> OrderResult:
            await asyncio.sleep(0.20)  # in-flight LONGER than the (shrunk) cooldown
            self.placed.append(request)
            return OrderResult(order_id="x", status=OrderStatus.SUBMITTED,
                               symbol=request.symbol, side=request.side)

    market_data = _FakeMarketData()
    inner = _SlowInner()
    broker = GuardedBroker(
        inner, market_data, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    async def _second():
        await asyncio.sleep(0.10)  # arrives after the cooldown but while #1 is still in-flight
        return await app.close_position("AAPL")

    _first, second = await asyncio.gather(app.close_position("AAPL"), _second())
    assert second["data"]["closed"] is False  # in-flight reservation was NOT evicted
    assert len(inner.placed) == 1
    app._recent_closes.clear()


async def test_close_position_releases_cooldown_on_rejected(tmp_path, monkeypatch):
    app._recent_closes.clear()

    class _RejectingInner(_FakeInner):
        async def place_order(self, request: OrderRequest) -> OrderResult:
            self.placed.append(request)
            return OrderResult(order_id=None, status=OrderStatus.REJECTED,
                               symbol=request.symbol, side=request.side)

    market_data = _FakeMarketData()
    inner = _RejectingInner()
    broker = GuardedBroker(
        inner, market_data, mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("1000"), require_market_open=False,
        journal=TradeJournal(tmp_path / "trades.jsonl"),
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=market_data, broker=broker, journal=broker._journal)
    monkeypatch.setattr(app, "_services", svc)

    first = await app.close_position("AAPL")
    assert first["data"]["status"] == "rejected"
    # The reservation was released (nothing dispatched), so a retry is NOT cooldown-blocked.
    second = await app.close_position("AAPL")
    assert second["data"].get("closed") is not False
    assert len(inner.placed) == 2
    app._recent_closes.clear()


async def test_wait_for_fill_stops_on_inactive(monkeypatch):
    class _InactiveInner(_FakeInner):
        async def get_order_status(self, order_id: str) -> OrderResult:
            return OrderResult(order_id=order_id, status=OrderStatus.INACTIVE,
                               symbol="AAPL", side=OrderSide.BUY)

    broker = GuardedBroker(
        _InactiveInner(), _FakeMarketData(), mode=TradingMode.PAPER, allow_live=False,
        dry_run=True, max_order_value=Decimal("1000"), require_market_open=False,
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=_FakeMarketData(), broker=broker,
                   journal=TradeJournal("logs/unused.jsonl"))
    monkeypatch.setattr(app, "_services", svc)

    out = await app.wait_for_fill("x", timeout_seconds=5)
    assert out["data"]["status"] == "inactive"
    assert out["data"]["timed_out"] is False


async def test_wait_for_fill_returns_on_terminal_status(monkeypatch):
    # _FakeInner.get_order_status returns FILLED, so it resolves on the first poll.
    broker = GuardedBroker(
        _FakeInner(), _FakeMarketData(), mode=TradingMode.PAPER, allow_live=False,
        dry_run=True, max_order_value=Decimal("1000"), require_market_open=False,
    )
    svc = Services(settings=Settings(ibkr_account_id="DU1"), client=None, auth=_FakeAuth(),
                   market_data=_FakeMarketData(), broker=broker,
                   journal=TradeJournal("logs/unused.jsonl"))
    monkeypatch.setattr(app, "_services", svc)

    out = await app.wait_for_fill("x", timeout_seconds=5)
    assert out["ok"] is True
    assert out["data"]["status"] == "filled"
    assert out["data"]["timed_out"] is False
