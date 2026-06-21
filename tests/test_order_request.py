from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibkr_agent.domain.models import OrderRequest, OrderSide, OrderType


def test_quantity_order_is_valid_and_not_fractional():
    order = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=2)
    assert order.is_fractional is False


def test_cash_qty_order_is_fractional():
    order = OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("50"))
    assert order.is_fractional is True


def test_rejects_both_quantity_and_cash_qty():
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=2, cash_qty=Decimal("50"))


def test_rejects_neither_sizing():
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY)


def test_limit_order_requires_limit_price():
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1, order_type=OrderType.LIMIT)
