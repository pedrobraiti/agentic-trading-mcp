"""Execução de ordens via CPAPI, com o loop de confirmação (reply) tratado.

A CPAPI raramente aceita a ordem de primeira: ela costuma responder com perguntas
de precaução (cada uma com `id` + `message` + `messageIds`). Precisamos confirmar
via `POST /iserver/reply/{id}` — possivelmente em várias rodadas. Por segurança, só
auto-confirmamos warnings cujo `messageId` está numa allow-list; qualquer warning
desconhecido BLOQUEIA a ordem (em vez de confirmar às cegas).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ...domain.models import OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from .client import CpapiClient, CpapiError

# Warnings benignos aceitos por padrão — confirmações de precaução padrão da CPAPI
# para o nosso tipo de ordem (MKT + cashQty). A própria API marca todos como
# isSuppressible=true / "Accept and Continue". Mapeados ao vivo na conta real:
#   o354   "order without market data" (sem subscrição de dados)
#   o10164 Market Order Confirmation (risco da ordem a mercado — usamos MKT de propósito)
#   o10223 Confirm Mandatory Cap Price (IB pode aplicar teto/piso de proteção)
#   o10151 disclaimer: responsabilidade do trader sobre detalhes de cash quantity
#   o10153 Cash Quantity Order Confirmation (cashQty é simulado: cancela ao gastar o valor)
DEFAULT_ACCEPTED_MESSAGE_IDS = frozenset(
    {"o354", "o10164", "o10223", "o10151", "o10153"}
)

_MAX_REPLY_ROUNDS = 5

_STATUS_MAP = {
    "submitted": OrderStatus.SUBMITTED,
    "presubmitted": OrderStatus.PENDING,
    "pendingsubmit": OrderStatus.PENDING,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


class CpapiBroker:
    """Implementa ``BrokerPort`` sobre a CPAPI."""

    def __init__(
        self,
        client: CpapiClient,
        account_id: str,
        resolve_conid: Callable[[str], Awaitable[int | None]],
        *,
        accepted_message_ids: frozenset[str] = DEFAULT_ACCEPTED_MESSAGE_IDS,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self._client = client
        self._account_id = account_id
        self._resolve_conid = resolve_conid
        self._accepted = accepted_message_ids
        self._id_factory = id_factory

    async def place_order(self, request: OrderRequest) -> OrderResult:
        conid = await self._resolve_conid(request.symbol)
        if conid is None:
            raise CpapiError(f"Não foi possível resolver o conid de {request.symbol}.")

        payload = {"orders": [self._build_order(request, conid)]}
        response = await self._client.post(
            f"/iserver/account/{self._account_id}/orders", json=payload
        )
        response = await self._resolve_replies(response)
        return self._parse_ack(response, request)

    async def cancel_order(self, order_id: str) -> OrderResult:
        response = await self._client.delete(
            f"/iserver/account/{self._account_id}/order/{order_id}"
        )
        message = response.get("msg") if isinstance(response, dict) else str(response)
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            symbol="",
            side=OrderSide.SELL,
            message=message,
            raw=response if isinstance(response, dict) else None,
        )

    async def get_live_orders(self) -> list[OrderResult]:
        # Mesmo padrão de warmup do snapshot: a 1ª chamada instancia, a 2ª traz dados.
        await self._client.get("/iserver/account/orders")
        data = await self._client.get("/iserver/account/orders")
        orders = data.get("orders", []) if isinstance(data, dict) else []
        return [_live_order_to_result(o) for o in orders]

    def _build_order(self, request: OrderRequest, conid: int) -> dict:
        order: dict = {
            "conid": conid,
            "orderType": request.order_type.value,
            "side": request.side.value,
            "tif": "DAY",
            "cOID": self._id_factory(),
        }
        if request.cash_qty is not None:
            order["cashQty"] = float(request.cash_qty)
        else:
            order["quantity"] = request.quantity
        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            order["price"] = float(request.limit_price)
        return order

    async def _resolve_replies(self, response: object) -> object:
        for _ in range(_MAX_REPLY_ROUNDS):
            question = _as_question(response)
            if question is None:
                return response

            message_ids = set(question.get("messageIds") or [])
            if not message_ids or not message_ids.issubset(self._accepted):
                texts = "; ".join(question.get("message") or [])
                ids = message_ids or "(sem id)"
                raise CpapiError(
                    f"Ordem bloqueada por warning não aprovado {ids}: {texts}",
                    payload=question,
                )
            response = await self._client.post(
                f"/iserver/reply/{question['id']}", json={"confirmed": True}
            )

        raise CpapiError("Excesso de rodadas de confirmação da CPAPI; ordem abortada.")

    def _parse_ack(self, response: object, request: OrderRequest) -> OrderResult:
        ack = response[0] if isinstance(response, list) and response else response
        if isinstance(ack, dict) and ack.get("order_id"):
            return OrderResult(
                order_id=str(ack["order_id"]),
                status=_map_status(ack.get("order_status")),
                symbol=request.symbol.upper(),
                side=request.side,
                message=ack.get("text"),
                raw=ack,
            )
        return OrderResult(
            status=OrderStatus.REJECTED,
            symbol=request.symbol.upper(),
            side=request.side,
            message=f"Resposta inesperada da CPAPI: {ack}",
            raw=ack if isinstance(ack, dict) else None,
        )


def _live_order_to_result(order: dict) -> OrderResult:
    side_raw = str(order.get("side", "")).upper()
    return OrderResult(
        order_id=str(order.get("orderId", "")),
        status=_map_status(order.get("status")),
        symbol=str(order.get("ticker", "")),
        side=OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL,
        filled_quantity=_dec(order.get("filledQuantity")),
        message=order.get("orderDesc"),
        raw=order,
    )


def _as_question(response: object) -> dict | None:
    if isinstance(response, list) and response and isinstance(response[0], dict):
        first = response[0]
        if "id" in first and "message" in first:
            return first
    return None


def _map_status(value: object) -> OrderStatus:
    return _STATUS_MAP.get(str(value or "").lower().replace(" ", ""), OrderStatus.UNKNOWN)


def _dec(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
