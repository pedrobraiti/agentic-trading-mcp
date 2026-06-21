"""Modelos de domínio — agnósticos ao broker concreto.

Valores monetários usam ``Decimal`` para evitar erro de ponto flutuante.
Uma ordem é expressa por ``quantity`` (ações inteiras) OU ``cash_qty`` (valor em
dólar, que habilita fracionário via CPAPI) — nunca os dois ao mesmo tempo.
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
    """Pedido de ordem. Exatamente um entre ``quantity`` e ``cash_qty`` deve ser informado."""

    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity: int | None = Field(default=None, gt=0, description="Ações inteiras.")
    cash_qty: Decimal | None = Field(
        default=None, gt=0, description="Valor em US$ (fracionário via cashQty)."
    )
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _exactly_one_sizing(self) -> OrderRequest:
        if (self.quantity is None) == (self.cash_qty is None):
            raise ValueError("Informe exatamente um entre 'quantity' e 'cash_qty'.")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("Ordem LIMIT exige 'limit_price'.")
        return self

    @property
    def is_fractional(self) -> bool:
        return self.cash_qty is not None


class OrderResult(BaseModel):
    """Resultado do envio de uma ordem ao broker."""

    order_id: str | None = None
    status: OrderStatus = OrderStatus.UNKNOWN
    symbol: str
    side: OrderSide
    filled_quantity: Decimal | None = None
    avg_price: Decimal | None = None
    message: str | None = None
    dry_run: bool = False
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
