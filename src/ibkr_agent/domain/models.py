"""Domain models — agnostic to the concrete broker.

Monetary values use ``Decimal`` to avoid floating-point error.
An order is expressed either by ``quantity`` (number of shares, fractional allowed) OR
``cash_qty`` (dollar amount) — never both at the same time.

CPAPI note: ``cash_qty`` (cashQty) is only accepted on BUYS. To sell/close a
fractional position you must use a fractional ``quantity`` (IBKR rejects
cashQty on sell orders). That is why ``quantity`` is ``Decimal``, not ``int``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MKT"
    LIMIT = "LMT"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    PENDING = "pending"
    UNKNOWN = "unknown"


class OrderRequest(BaseModel):
    """Order request. Exactly one of ``quantity`` and ``cash_qty`` must be provided."""

    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal | None = Field(
        default=None, gt=0, description="Number of shares (fractional allowed)."
    )
    cash_qty: Decimal | None = Field(
        default=None, gt=0, description="Amount in US$ (fractional via cashQty)."
    )
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _exactly_one_sizing(self) -> OrderRequest:
        if (self.quantity is None) == (self.cash_qty is None):
            raise ValueError("Provide exactly one of 'quantity' and 'cash_qty'.")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("A LIMIT order requires 'limit_price'.")
        return self

    @property
    def is_fractional(self) -> bool:
        return self.cash_qty is not None


class OrderResult(BaseModel):
    """Result of submitting an order to the broker."""

    order_id: str | None = None
    status: OrderStatus = OrderStatus.UNKNOWN
    symbol: str
    side: OrderSide
    filled_quantity: Decimal | None = None
    avg_price: Decimal | None = None
    message: str | None = None
    dry_run: bool = False
    raw: dict | None = None


class OrderPreview(BaseModel):
    """Estimated impact of an order before it is sent (IBKR whatif).

    Lets the agent reason about cost and margin before committing money.
    ``amount`` is the total cash outlay (incl. commission) and ``commission`` the
    estimated fee. ``margin_change``/``equity_change`` are populated for
    margin-affecting orders; for fractional cash orders IBKR instead reports the
    available-funds before/after. ``raw`` always carries the full payload.
    """

    symbol: str
    side: OrderSide
    commission: Decimal | None = None
    amount: Decimal | None = None
    margin_change: Decimal | None = None
    equity_change: Decimal | None = None
    available_funds_before: Decimal | None = None
    available_funds_after: Decimal | None = None
    warnings: list[str] = []
    raw: dict | None = None


class Quote(BaseModel):
    symbol: str
    conid: int
    last_price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


class Position(BaseModel):
    conid: int
    symbol: str
    quantity: Decimal
    avg_cost: Decimal | None = None
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None


class AccountSummary(BaseModel):
    account_id: str
    available_funds: Decimal | None = None
    net_liquidation: Decimal | None = None
    buying_power: Decimal | None = None
    currency: str = "USD"
