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
    STOP = "STP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAIL = "TRAIL"


class TrailingType(StrEnum):
    AMOUNT = "amt"
    PERCENT = "%"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    PENDING = "pending"
    # CPAPI's terminal-but-not-filled state (a rejected/parked/dead order). Distinct from
    # REJECTED because it can also mean "parked until the market opens"; either way it is
    # not actively working, so wait_for_fill should stop rather than poll to the timeout.
    INACTIVE = "inactive"
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
    stop_price: Decimal | None = Field(
        default=None, gt=0, description="Trigger price for STOP / STOP_LIMIT orders."
    )
    trailing_amount: Decimal | None = Field(
        default=None, gt=0, description="Trail distance for a TRAIL order ($ or %)."
    )
    trailing_type: TrailingType = TrailingType.AMOUNT

    @model_validator(mode="after")
    def _validate(self) -> OrderRequest:
        if (self.quantity is None) == (self.cash_qty is None):
            raise ValueError("Provide exactly one of 'quantity' and 'cash_qty'.")
        # cashQty is simulated by IBKR for MARKET BUYS only — it is rejected on sells and
        # on limit/stop orders. Enforce it here so a malformed cash order can't be built
        # and slip past the side-specific guards (a cash SELL has no quantity, which would
        # otherwise bypass the naked-short check entirely).
        if self.cash_qty is not None and (
            self.side is not OrderSide.BUY or self.order_type is not OrderType.MARKET
        ):
            raise ValueError(
                "cash_qty is only valid for a MARKET BUY; sells, limits and stops must "
                "use 'quantity'."
            )
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"A {self.order_type.value} order requires 'limit_price'.")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"A {self.order_type.value} order requires 'stop_price'.")
        if self.order_type is OrderType.TRAIL and self.trailing_amount is None:
            raise ValueError("A TRAIL order requires 'trailing_amount'.")
        return self

    @property
    def is_fractional(self) -> bool:
        return self.cash_qty is not None


class BracketRequest(BaseModel):
    """An entry order with attached take-profit and stop-loss exits (OCO).

    The two exits are submitted as children of the entry: when one fills the other
    is cancelled. The entry must be sized by ``quantity`` (not ``cash_qty``) — the
    exits need a definite share count, which a dollar-amount entry can't give until
    it fills.
    """

    entry: OrderRequest
    take_profit_price: Decimal = Field(gt=0, description="Limit price of the profit exit.")
    stop_loss_price: Decimal = Field(gt=0, description="Stop trigger price of the loss exit.")

    @model_validator(mode="after")
    def _entry_and_exits_consistent(self) -> BracketRequest:
        if self.entry.quantity is None:
            raise ValueError("A bracket entry must be sized by 'quantity', not 'cash_qty'.")
        # The take-profit and stop-loss must sit on the correct sides, or the bracket
        # liquidates the moment the entry fills. For a BUY entry (exits are SELLs):
        # take_profit ABOVE, stop_loss BELOW (and below the entry limit, if any). A SELL
        # entry is the mirror image.
        if self.entry.side is OrderSide.BUY:
            if self.take_profit_price <= self.stop_loss_price:
                raise ValueError("For a BUY bracket, take_profit must be above stop_loss.")
            limit = self.entry.limit_price
            if limit is not None and not (self.take_profit_price > limit > self.stop_loss_price):
                raise ValueError(
                    "For a BUY bracket: take_profit > entry limit_price > stop_loss."
                )
        else:
            if self.take_profit_price >= self.stop_loss_price:
                raise ValueError("For a SELL bracket, take_profit must be below stop_loss.")
            limit = self.entry.limit_price
            if limit is not None and not (self.take_profit_price < limit < self.stop_loss_price):
                raise ValueError(
                    "For a SELL bracket: take_profit < entry limit_price < stop_loss."
                )
        return self


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
