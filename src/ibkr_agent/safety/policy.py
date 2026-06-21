"""Travas de segurança sobre a execução de ordens.

``GuardedBroker`` é um decorator de ``BrokerPort``: aplica as regras duras ANTES de
delegar ao broker real. Regras:
  1. Live só com ``allow_live=True`` (senão bloqueia).
  2. Notional da ordem não pode passar de ``max_order_value``.
  3. Mercado precisa estar aberto (RTH), se exigido.
  4. ``dry_run`` (padrão): valida tudo mas NÃO envia a ordem — retorna um
     ``OrderResult`` marcado com ``dry_run=True``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from ..domain.models import OrderRequest, OrderResult, OrderStatus, TradingMode
from ..domain.ports import BrokerPort, MarketDataPort


class SafetyError(Exception):
    """Ordem viola uma regra de segurança — não deve ser enviada."""


class GuardedBroker:
    """Implementa ``BrokerPort`` envolvendo outro ``BrokerPort`` com travas."""

    def __init__(
        self,
        inner: BrokerPort,
        market_data: MarketDataPort,
        *,
        mode: TradingMode,
        allow_live: bool,
        dry_run: bool,
        max_order_value: Decimal,
        require_market_open: bool = True,
        is_market_open: Callable[[], bool] = lambda: True,
    ):
        self._inner = inner
        self._market_data = market_data
        self._mode = mode
        self._allow_live = allow_live
        self._dry_run = dry_run
        self._max_order_value = max_order_value
        self._require_market_open = require_market_open
        self._is_market_open = is_market_open

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if self._mode is TradingMode.LIVE and not self._allow_live:
            raise SafetyError(
                "Modo LIVE bloqueado: defina TRADING_ALLOW_LIVE=true para operar com dinheiro real."
            )

        if self._require_market_open and not self._is_market_open():
            raise SafetyError("Mercado fechado: ordens só são aceitas durante o pregão (RTH).")

        notional = await self._notional(request)
        if notional is not None and notional > self._max_order_value:
            raise SafetyError(
                f"Ordem de ~US${notional} excede o limite MAX_ORDER_VALUE "
                f"(US${self._max_order_value})."
            )

        if self._dry_run:
            return OrderResult(
                status=OrderStatus.PENDING,
                symbol=request.symbol.upper(),
                side=request.side,
                dry_run=True,
                message=f"dry-run: ordem validada, NÃO enviada (notional ~US${notional}).",
            )

        return await self._inner.place_order(request)

    async def cancel_order(self, order_id: str) -> OrderResult:
        return await self._inner.cancel_order(order_id)

    async def get_live_orders(self) -> list[OrderResult]:
        return await self._inner.get_live_orders()

    async def _notional(self, request: OrderRequest) -> Decimal | None:
        """Valor estimado da ordem em US$. Para cashQty é direto; para quantity usa cotação."""
        if request.cash_qty is not None:
            return request.cash_qty

        quote = await self._market_data.get_quote(request.symbol)
        price = quote.last_price if quote else None
        if price is None:
            raise SafetyError(
                f"Sem preço para {request.symbol}: não dá para validar o limite de valor da ordem."
            )
        return price * Decimal(request.quantity or 0)
