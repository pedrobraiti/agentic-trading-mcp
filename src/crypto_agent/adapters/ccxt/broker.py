"""Order execution via CCXT (spot): market/limit buys and sells, native stops, cancel, open orders.

Buy-by-value (the crypto analogue of IBKR's cashQty) prefers the exchange-native
``createMarketBuyOrderWithCost`` and falls back to ``cost / price`` rounded to the
market's precision. Stops are EXCHANGE-NATIVE trigger orders (CCXT's unified
``triggerPrice``) where the exchange supports them — a resting stop survives the skill
not running, unlike the old skill-monitored soft stop. Brackets/previews are not
offered on this venue.
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
        if request.order_type not in (
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.STOP,
            OrderType.STOP_LIMIT,
        ):
            raise CryptoExchangeError(
                "The crypto venue supports MARKET, LIMIT and STOP/STOP_LIMIT orders only "
                "(no native trailing/bracket)."
            )
        side = request.side.value.lower()
        # Pass the safety layer's cOID to the exchange as a clientOrderId (CCXT unifies this
        # via params) so the journaled intent and the live order share an idempotency key —
        # what lets a timed-out dispatch be reconciled against the venue's open orders.
        params = {"clientOrderId": request.client_order_id} if request.client_order_id else {}

        if request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            order = await self._place_stop(symbol, side, request, params)
        elif request.cash_qty is not None:
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

    async def _place_stop(
        self, symbol: str, side: str, request: OrderRequest, params: dict
    ) -> dict:
        """Exchange-native stop via CCXT's unified ``triggerPrice``. Fail-closed on capability.

        A stop with a ``limit_price`` becomes a stop-LIMIT (a resting trigger that places a
        limit order when ``stop_price`` trades); without one it would be a stop-MARKET, which
        many spot APIs (binance spot included) do NOT offer — that case is refused with
        guidance instead of silently substituting a different order type. Stops are sized by
        base ``quantity`` only (a trigger order cannot be sized by quote cost).
        """
        if request.cash_qty is not None:
            raise CryptoExchangeError(
                "Stop orders are sized by base 'quantity', not by quote-currency value."
            )
        has = self._ex.has
        if request.limit_price is None:
            if not has.get("createStopMarketOrder"):
                raise CryptoExchangeError(
                    f"{self._ex.id} spot does not support stop-MARKET orders; pass "
                    "'limit_price' to place a stop-LIMIT instead (for a SELL stop, set it "
                    "at or slightly below 'stop_price' — mind that a limit may not fill "
                    "through a violent gap)."
                )
        elif not (
            has.get("createStopLimitOrder")
            or has.get("createStopOrder")
            or has.get("createTriggerOrder")
        ):
            raise CryptoExchangeError(
                f"{self._ex.id} does not support exchange-native stop orders; the only "
                "protection available here is a skill-monitored soft stop."
            )
        amount = self._client.amount_to_precision(symbol, request.quantity or Decimal(0))
        reference_price = request.limit_price or request.stop_price
        self._client.validate_limits(symbol, amount, reference_price)
        order_type = "limit" if request.limit_price is not None else "market"
        stop_params = {**params, "triggerPrice": float(request.stop_price)}
        return await self._ex.create_order(
            symbol,
            order_type,
            side,
            float(amount),
            float(request.limit_price) if request.limit_price is not None else None,
            stop_params,
        )

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
