from decimal import Decimal

import pytest

from ibkr_agent.domain.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
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

    async def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.CANCELLED,
                           symbol="", side=OrderSide.SELL)

    async def get_live_orders(self) -> list[OrderResult]:
        return []


class FakeMarketData:
    def __init__(self, price: Decimal | None):
        self._price = price

    async def resolve_conid(self, symbol: str) -> int | None:
        return 1

    async def get_quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, conid=1, last_price=self._price)

    async def get_account_summary(self):
        raise NotImplementedError

    async def get_positions(self):
        return []


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
    with pytest.raises(SafetyError, match="excede"):
        await guarded.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("500"))
        )


async def test_quantity_notional_uses_quote():
    broker = FakeBroker()
    guarded = _guarded(broker, FakeMarketData(Decimal("60")))  # 2 * 60 = 120 > 100
    with pytest.raises(SafetyError, match="excede"):
        await guarded.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=2))
    assert broker.placed == []


async def test_market_closed_blocks():
    guarded = _guarded(
        FakeBroker(), FakeMarketData(Decimal("10")),
        require_market_open=True, is_market_open=lambda: False,
    )
    with pytest.raises(SafetyError, match="fechado"):
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
