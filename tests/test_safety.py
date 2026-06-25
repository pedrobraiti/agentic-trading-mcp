from decimal import Decimal

import pytest

from ibkr_agent.domain.models import (
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    TradingMode,
)
from ibkr_agent.safety import GuardedBroker, SafetyError


class FakeBroker:
    def __init__(self):
        self.placed: list[OrderRequest] = []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.placed.append(request)
        return OrderResult(order_id="real-1", status=OrderStatus.SUBMITTED,
                           symbol=request.symbol, side=request.side)

    async def place_bracket(self, bracket) -> list[OrderResult]:
        self.placed.append(bracket.entry)
        return [OrderResult(order_id="b-1", status=OrderStatus.SUBMITTED,
                            symbol=bracket.entry.symbol, side=bracket.entry.side)]

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        return OrderPreview(symbol=request.symbol, side=request.side)

    async def get_order_status(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.FILLED,
                           symbol="", side=OrderSide.BUY)

    async def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.CANCELLED,
                           symbol="", side=OrderSide.SELL)

    async def get_live_orders(self) -> list[OrderResult]:
        return []


class FakeMarketData:
    def __init__(self, price: Decimal | None, *, held: Decimal = Decimal("0")):
        self._price = price
        self._held = held

    async def resolve_conid(self, symbol: str) -> int | None:
        return 1

    async def get_quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, conid=1, last_price=self._price)

    async def get_account_summary(self):
        raise NotImplementedError

    async def get_positions(self):
        if self._held == 0:
            return []
        return [Position(conid=1, symbol="AAPL", quantity=self._held)]

    async def invalidate_positions(self) -> None: ...


def _guarded(broker, md, **kw):
    defaults = dict(
        mode=TradingMode.PAPER, allow_live=False, dry_run=False,
        max_order_value=Decimal("100"), require_market_open=False,
        is_market_open=lambda: True,
    )
    defaults.update(kw)
    return GuardedBroker(broker, md, **defaults)


async def test_dry_run_does_not_send():
    broker, md = FakeBroker(), FakeMarketData(Decimal("10"))
    guarded = _guarded(broker, md, dry_run=True)

    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )

    assert result.dry_run is True
    assert broker.placed == []


async def test_live_blocked_without_allow_live():
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")), mode=TradingMode.LIVE)
    with pytest.raises(SafetyError, match="LIVE"):
        await guarded.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("10"))
        )


async def test_cashqty_over_limit_blocked():
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")))
    with pytest.raises(SafetyError, match="exceeds"):
        await guarded.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("500"))
        )


async def test_quantity_notional_uses_quote():
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("60")))  # 2 * 60 = 120 > 100
    with pytest.raises(SafetyError, match="exceeds"):
        await guarded.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=2))
    assert broker.placed == []


async def test_sell_over_limit_is_allowed():
    # The value cap is a spend limit: it gates BUYS. A SELL/exit larger than the
    # limit must NOT be blocked, or a big position couldn't be closed or protected.
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("60"), held=Decimal("5")))  # 5*60=300>100
    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=5)
    )
    assert result.order_id == "real-1"
    assert len(broker.placed) == 1


async def test_stop_loss_over_limit_is_allowed():
    from ibkr_agent.domain.models import OrderType

    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("60"), held=Decimal("5")))  # 5*60=300>100
    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=5,
                     order_type=OrderType.STOP, stop_price=Decimal("55"))
    )
    assert result.order_id == "real-1"
    assert len(broker.placed) == 1


async def test_buy_blocked_when_quote_missing_for_quantity():
    # Without a price the notional can't be validated, so a quantity BUY must be blocked
    # (the daily cap relies on the same notional) — fail safe rather than send blind.
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(None))
    with pytest.raises(SafetyError, match="No usable price"):
        await guarded.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1))
    assert broker.placed == []


async def test_market_closed_blocks():
    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        require_market_open=True, is_market_open=lambda: False,
    )
    with pytest.raises(SafetyError, match="closed"):
        await guarded.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("10"))
        )


async def test_happy_path_delegates_to_inner():
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("10")))

    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )

    assert result.order_id == "real-1"
    assert len(broker.placed) == 1


def _buy(symbol: str, amount: str) -> OrderRequest:
    return OrderRequest(symbol=symbol, side=OrderSide.BUY, cash_qty=Decimal(amount))


async def test_daily_limit_blocks_after_cumulative_spend(tmp_path):
    from ibkr_agent.journal import TradeJournal

    journal = TradeJournal(tmp_path / "trades.jsonl")
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")),
                       journal=journal, max_daily_value=Decimal("40"))

    await guarded.place_order(_buy("AAPL", "30"))
    with pytest.raises(SafetyError, match="Daily spend limit"):
        await guarded.place_order(_buy("MSFT", "20"))


async def test_duplicate_order_blocked(tmp_path):
    from ibkr_agent.journal import TradeJournal

    journal = TradeJournal(tmp_path / "trades.jsonl")
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")),
                       journal=journal, duplicate_window_seconds=30)

    await guarded.place_order(_buy("AAPL", "10"))
    with pytest.raises(SafetyError, match="Duplicate"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_symbol_denylist_blocks():
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")),
                       symbol_denylist=frozenset({"TSLA"}))
    with pytest.raises(SafetyError, match="deny-list"):
        await guarded.place_order(_buy("tsla", "10"))


async def test_symbol_allowlist_blocks_others_and_allows_listed():
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10")),
                       symbol_allowlist=frozenset({"AAPL"}))
    with pytest.raises(SafetyError, match="allow-list"):
        await guarded.place_order(_buy("MSFT", "10"))
    result = await guarded.place_order(_buy("AAPL", "10"))
    assert result.order_id == "real-1"


def _account(account_id: str, is_paper: bool):
    async def provider() -> dict:
        return {"account_id": account_id, "is_paper": is_paper,
                "account_type": "PAPER" if is_paper else "LIVE"}
    return provider


async def test_real_account_blocked_without_allow_live():
    # Ground truth wins over the label: a real account with allow_live off is refused
    # even though the config says paper.
    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        account_info_provider=_account("U1", is_paper=False),
        configured_account_id="U1", mode=TradingMode.PAPER, allow_live=False,
    )
    with pytest.raises(SafetyError, match="LIVE account detected"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_real_account_blocked_when_label_says_paper():
    # allow_live is on, but the label still disagrees with the real account → refuse.
    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        account_info_provider=_account("U1", is_paper=False),
        configured_account_id="U1", mode=TradingMode.PAPER, allow_live=True,
    )
    with pytest.raises(SafetyError, match="disagrees with reality"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_real_account_allowed_when_armed_and_consistent():
    broker = FakeBroker()
    guarded = _guarded(
        broker, FakeMarketData(Decimal("10")),
        account_info_provider=_account("U1", is_paper=False),
        configured_account_id="U1", mode=TradingMode.LIVE, allow_live=True,
    )
    result = await guarded.place_order(_buy("AAPL", "10"))
    assert result.order_id == "real-1"


async def test_account_id_mismatch_blocked():
    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        account_info_provider=_account("U999", is_paper=True),
        configured_account_id="DU1",
    )
    with pytest.raises(SafetyError, match="Account mismatch"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_naked_short_blocked_and_allowed_with_flag():
    # Selling more than held opens a short → blocked by default.
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("10"), held=Decimal("2")))
    with pytest.raises(SafetyError, match="open a short"):
        await guarded.place_order(OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=3))

    broker = FakeBroker()
    allowed = _guarded(broker, FakeMarketData(Decimal("10"), held=Decimal("2")),
                       allow_short=True)
    result = await allowed.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=3)
    )
    assert result.order_id == "real-1"


async def test_inverted_stop_blocked():
    from ibkr_agent.domain.models import OrderType

    # SELL stop AT/ABOVE the market (last=50) would fire instantly — fat-finger.
    guarded = _guarded(FakeBroker(), FakeMarketData(Decimal("50"), held=Decimal("5")))
    with pytest.raises(SafetyError, match="trigger immediately"):
        await guarded.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=1,
                         order_type=OrderType.STOP, stop_price=Decimal("55"))
        )


async def test_unknown_paper_status_fails_closed():
    # If IBKR can't confirm paper vs live (is_paper None), refuse to trade.
    async def provider():
        return {"account_id": "U1", "is_paper": None, "account_type": None}

    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        account_info_provider=provider, configured_account_id="U1",
    )
    with pytest.raises(SafetyError, match="PAPER or a LIVE"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_exit_not_trapped_when_no_quote():
    # A SELL must never be blocked just because there's no price — exits can't be trapped.
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(None, held=Decimal("5")))
    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=5)
    )
    assert result.order_id == "real-1"


async def test_value_cap_not_bypassed_by_zero_price():
    # A zero/garbage price must not make the notional 0 and slip past the cap.
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("0")))
    with pytest.raises(SafetyError, match="No usable price"):
        await guarded.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=100))
    assert broker.placed == []


async def test_bracket_take_profit_inverted_vs_market_blocked():
    from ibkr_agent.domain.models import BracketRequest

    # BUY bracket, take_profit (49) BELOW the market (last=50): the SELL limit would fill
    # instantly when the entry fills. (tp=49 > sl=10 passes the model; the guard catches it.)
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("50")))
    bracket = BracketRequest(
        entry=OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1),
        take_profit_price=Decimal("49"),
        stop_loss_price=Decimal("10"),
    )
    with pytest.raises(SafetyError, match="fill immediately"):
        await guarded.place_bracket(bracket)
    assert broker.placed == []


async def test_transient_account_read_failure_does_not_trap_after_verified():
    # First order verifies the account; a later transient read failure falls back to the
    # cached identity instead of trapping the order (especially an exit).
    calls = {"n": 0}

    async def provider():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"account_id": "U1", "is_paper": False, "account_type": "LIVE"}
        raise RuntimeError("transient /iserver/accounts 503")

    broker = FakeBroker()
    guarded = _guarded(
        broker, FakeMarketData(Decimal("10"), held=Decimal("5")),
        account_info_provider=provider, configured_account_id="U1",
        mode=TradingMode.LIVE, allow_live=True,
    )
    await guarded.place_order(_buy("AAPL", "10"))  # verifies + caches
    # Provider now raises, but a SELL exit must still go through on the cached identity.
    result = await guarded.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=1)
    )
    assert result.order_id == "real-1"


async def test_account_read_failure_with_no_prior_verification_fails_closed():
    async def provider():
        raise RuntimeError("503")

    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        account_info_provider=provider, configured_account_id="U1",
    )
    with pytest.raises(SafetyError, match="no prior confirmation"):
        await guarded.place_order(_buy("AAPL", "10"))


async def test_concurrent_identical_buys_are_serialized(tmp_path):
    # Two parallel buys must not both slip past the duplicate guard (the check-then-record
    # critical section is locked). Exactly one goes through; the other is blocked.
    import asyncio

    from ibkr_agent.journal import TradeJournal

    journal = TradeJournal(tmp_path / "t.jsonl")
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("10")),
                       journal=journal, duplicate_window_seconds=30)
    results = await asyncio.gather(
        guarded.place_order(_buy("AAPL", "10")),
        guarded.place_order(_buy("AAPL", "10")),
        return_exceptions=True,
    )
    assert len(broker.placed) == 1  # the lock stopped the double-send
    assert sum(isinstance(r, SafetyError) for r in results) == 1


async def test_limit_entry_bracket_not_blocked_by_market_check():
    from ibkr_agent.domain.models import BracketRequest, OrderType

    # "Buy the dip": LIMIT entry @90, take_profit 95, stop_loss 85 — valid vs the 90 fill,
    # even though 95 is below the current market (100). Must NOT be blocked.
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("100"), held=Decimal("0")))
    bracket = BracketRequest(
        entry=OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1,
                           order_type=OrderType.LIMIT, limit_price=Decimal("90")),
        take_profit_price=Decimal("95"),
        stop_loss_price=Decimal("85"),
    )
    await guarded.place_bracket(bracket)
    assert len(broker.placed) == 1


async def test_bracket_stop_loss_inverted_vs_market_blocked():
    from ibkr_agent.domain.models import BracketRequest

    # BUY bracket, but stop_loss (95) is ABOVE the market (last=50): the SELL stop would
    # fire instantly when the entry fills. (tp>sl passes the model; the guard catches this.)
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("50")))
    bracket = BracketRequest(
        entry=OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1),
        take_profit_price=Decimal("120"),
        stop_loss_price=Decimal("95"),
    )
    with pytest.raises(SafetyError, match="trigger immediately"):
        await guarded.place_bracket(bracket)
    assert broker.placed == []
