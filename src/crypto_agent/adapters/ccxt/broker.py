"""Order execution via CCXT (spot): market/limit buys and sells, cancel, open orders.

Buy-by-value (the crypto analogue of IBKR's cashQty) prefers the exchange-native
``createMarketBuyOrderWithCost`` and falls back to ``cost / price`` rounded to the
market's precision. Brackets/stops/previews are not offered on this venue.
"""

from __future__ import annotations

from decimal import Decimal

from trading_core.domain.models import (
    BracketRequest,
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)

from .client import CcxtClient, CryptoExchangeError, to_decimal

# CCXT order 'status' → our domain OrderStatus.
_STATUS_MAP = {
    "open": OrderStatus.SUBMITTED,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.INACTIVE,
}


class CcxtBroker:
    """Implements ``BrokerPort`` on top of a CCXT exchange (spot, no leverage)."""

    def __init__(self, client: CcxtClient):
        self._client = client
        # order_id → symbol, so cancel/status work without the caller passing a symbol
        # (CCXT needs the symbol; the IBKR-shaped tools only pass an id).
        self._symbol_by_id: dict[str, str] = {}

    @property
    def _ex(self):
        return self._client.exchange

    async def place_order(self, request: OrderRequest) -> OrderResult:
        await self._client.ensure_markets()
        symbol = self._client.normalize_symbol(request.symbol)
        if request.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise CryptoExchangeError(
                "The crypto venue supports MARKET and LIMIT orders only "
                "(no native stop/trailing/bracket)."
            )
        side = request.side.value.lower()
        # Pass the safety layer's cOID to the exchange as a clientOrderId (CCXT unifies this
        # via params) so the journaled intent and the live order share an idempotency key —
        # what lets a timed-out dispatch be reconciled against the venue's open orders.
        params = {"clientOrderId": request.client_order_id} if request.client_order_id else {}

        if request.cash_qty is not None:
            order = await self._buy_by_value(symbol, request.cash_qty, params)
        else:
            amount = self._client.amount_to_precision(symbol, request.quantity or Decimal(0))
            price = request.limit_price
            self._client.validate_limits(symbol, amount, price)
            order_type = "limit" if request.order_type is OrderType.LIMIT else "market"
            order = await self._ex.create_order(
                symbol,
                order_type,
                side,
                float(amount),
                float(price) if price is not None else None,
                params,
            )
        return self._to_result(order, fallback_symbol=symbol, fallback_side=request.side)

    async def _buy_by_value(self, symbol: str, cost: Decimal, params: dict | None = None) -> dict:
        """Market BUY for a quote-currency amount (cashQty analogue)."""
        params = params or {}
        self._client.validate_cost(symbol, cost)
        if self._ex.has.get("createMarketBuyOrderWithCost"):
            return await self._ex.create_market_buy_order_with_cost(symbol, float(cost), params)
        # Fallback: size the base amount from the live price, then round to precision.
        ticker = await self._ex.fetch_ticker(symbol)
        last = to_decimal(ticker.get("last") or ticker.get("close"))
        if last is None or last <= 0:
            raise CryptoExchangeError(
                f"No usable price for {symbol}; cannot size a buy-by-value order."
            )
        amount = self._client.amount_to_precision(symbol, cost / last)
        self._client.validate_limits(symbol, amount, last)
        return await self._ex.create_order(symbol, "market", "buy", float(amount), None, params)

    async def cancel_order(self, order_id: str) -> OrderResult:
        symbol = await self._symbol_for(order_id)
        order = await self._ex.cancel_order(order_id, symbol)
        return self._to_result(order, fallback_symbol=symbol, fallback_side=None)

    async def get_order_status(self, order_id: str) -> OrderResult:
        symbol = await self._symbol_for(order_id)
        order = await self._ex.fetch_order(order_id, symbol)
        return self._to_result(order, fallback_symbol=symbol, fallback_side=None)

    async def get_live_orders(self) -> list[OrderResult]:
        await self._client.ensure_markets()
        orders = await self._ex.fetch_open_orders()
        results: list[OrderResult] = []
        for order in orders:
            results.append(self._to_result(order, fallback_symbol=None, fallback_side=None))
        return results

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        raise CryptoExchangeError(
            "preview_order is not available on the crypto venue (no whatif); use get_quote "
            "and account_summary to estimate cost before buying."
        )

    async def place_bracket(self, bracket: BracketRequest) -> list[OrderResult]:
        raise CryptoExchangeError(
            "Bracket/OCO orders are not offered on the crypto venue (spot, no native OCO)."
        )

    async def _symbol_for(self, order_id: str) -> str:
        symbol = self._symbol_by_id.get(order_id)
        if symbol is None:
            await self.get_live_orders()  # repopulate the cache from the exchange
            symbol = self._symbol_by_id.get(order_id)
        if symbol is None:
            raise CryptoExchangeError(
                f"Unknown order '{order_id}': it is not among this session's open orders. "
                "Use open_orders to list active orders."
            )
        return symbol

    def _to_result(
        self,
        order: dict,
        *,
        fallback_symbol: str | None,
        fallback_side: OrderSide | None,
    ) -> OrderResult:
        order_id = order.get("id")
        symbol = order.get("symbol") or fallback_symbol or ""
        if order_id and order.get("symbol"):
            self._symbol_by_id[str(order_id)] = order["symbol"]
        raw_side = order.get("side")
        side = (
            OrderSide(raw_side.upper())
            if isinstance(raw_side, str) and raw_side.upper() in OrderSide.__members__
            else (fallback_side or OrderSide.BUY)
        )
        status = _STATUS_MAP.get(order.get("status"), OrderStatus.UNKNOWN)
        return OrderResult(
            order_id=str(order_id) if order_id else None,
            status=status,
            symbol=symbol,
            side=side,
            filled_quantity=to_decimal(order.get("filled")),
            avg_price=to_decimal(order.get("average") or order.get("price")),
            message=order.get("status"),
            raw=order,
        )
