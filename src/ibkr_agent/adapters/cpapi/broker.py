"""Order execution via CPAPI, with the confirmation (reply) loop handled.

The CPAPI rarely accepts an order on the first try: it usually responds with
precaution questions (each with `id` + `message` + `messageIds`). We need to confirm
via `POST /iserver/reply/{id}` — possibly over several rounds. For safety, we only
auto-confirm warnings whose `messageId` is in an allow-list; any unknown warning
BLOCKS the order (instead of confirming blindly).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ...domain.models import (
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from .client import CpapiClient, CpapiError

# Benign warnings accepted by default — standard CPAPI precaution confirmations
# for our order type (MKT + cashQty). The API itself marks them all as
# isSuppressible=true / "Accept and Continue". Mapped live on the real account:
#   o354   "order without market data" (no data subscription)
#   o10164 Market Order Confirmation (market-order risk — we use MKT on purpose)
#   o10223 Confirm Mandatory Cap Price (IB may apply a protective cap/floor)
#   o10151 disclaimer: trader's responsibility over cash quantity details
#   o10153 Cash Quantity Order Confirmation (cashQty is simulated: cancels once the amount is spent)
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
    """Implements ``BrokerPort`` on top of the CPAPI."""

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
            raise CpapiError(f"Could not resolve the conid for {request.symbol}.")

        payload = {"orders": [self._build_order(request, conid)]}
        response = await self._client.post(
            f"/iserver/account/{self._account_id}/orders", json=payload
        )
        response = await self._resolve_replies(response)
        return self._parse_ack(response, request)

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        conid = await self._resolve_conid(request.symbol)
        if conid is None:
            raise CpapiError(f"Could not resolve the conid for {request.symbol}.")

        payload = {"orders": [self._build_order(request, conid)]}
        response = await self._client.post(
            f"/iserver/account/{self._account_id}/orders/whatif", json=payload
        )
        return _parse_preview(response, request)

    async def cancel_order(self, order_id: str) -> OrderResult:
        # Best-effort: resolve the symbol/side from live orders before cancelling.
        symbol, side = "", OrderSide.SELL
        try:
            for order in await self.get_live_orders():
                if order.order_id == str(order_id):
                    symbol, side = order.symbol, order.side
                    break
        except CpapiError:
            pass

        response = await self._client.delete(
            f"/iserver/account/{self._account_id}/order/{order_id}"
        )
        message = response.get("msg") if isinstance(response, dict) else str(response)
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            symbol=symbol,
            side=side,
            message=message,
            raw=response if isinstance(response, dict) else None,
        )

    async def get_live_orders(self) -> list[OrderResult]:
        # Same warmup pattern as the snapshot: the 1st call instantiates, the 2nd brings data.
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
            order["quantity"] = float(request.quantity)
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
                await self._decline(question["id"])
                texts = "; ".join(question.get("message") or [])
                ids = message_ids or "(no id)"
                raise CpapiError(
                    f"Order blocked by unapproved warning {ids}: {texts}",
                    payload=question,
                )
            response = await self._client.post(
                f"/iserver/reply/{question['id']}", json={"confirmed": True}
            )

        raise CpapiError("Too many CPAPI confirmation rounds; order aborted.")

    async def _decline(self, reply_id: str) -> None:
        """Decline the pending order (``confirmed: false``) so it isn't left `Inactive`.

        Best-effort: if the decline fails, we proceed to raise the blocking error —
        what matters is not confirming blindly, and the decline is just cleanup.
        """
        try:
            await self._client.post(f"/iserver/reply/{reply_id}", json={"confirmed": False})
        except CpapiError:
            pass

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
            message=f"Unexpected response from the CPAPI: {ack}",
            raw=ack if isinstance(ack, dict) else None,
        )


def _parse_preview(response: object, request: OrderRequest) -> OrderPreview:
    """Parse the whatif response; ``raw`` always carries the full payload.

    Validated against a live retail whatif: money fields arrive as unit-suffixed
    strings (``"2.02 USD"``), warnings live in ``warns`` as ``"<code>/<html>"``,
    and a cash order leaves ``initial``/``equity`` null while reporting the
    available-funds impact in the ``data`` rows.
    """
    data = response[0] if isinstance(response, list) and response else response
    data = data if isinstance(data, dict) else {}
    amount = data.get("amount") if isinstance(data.get("amount"), dict) else {}
    initial = data.get("initial") if isinstance(data.get("initial"), dict) else {}
    equity = data.get("equity") if isinstance(data.get("equity"), dict) else {}
    funds_before, funds_after = _funds_impact(data.get("data"))

    return OrderPreview(
        symbol=request.symbol.upper(),
        side=request.side,
        commission=_money(amount.get("commission")),
        amount=_money(amount.get("total") or amount.get("amount")),
        margin_change=_money(initial.get("change")),
        equity_change=_money(equity.get("change")),
        available_funds_before=funds_before,
        available_funds_after=funds_after,
        warnings=_preview_warnings(data),
        raw=data or None,
    )


_MONEY_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WARN_CODE_RE = re.compile(r"^\d+/")


def _money(value: object) -> Decimal | None:
    """Pull a number out of values like ``"2.02 USD"`` or ``"2 USD (0.0067 Shares)"``."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return _dec(value)
    match = _MONEY_RE.search(str(value))
    return _dec(match.group(0).replace(",", "")) if match else None


def _funds_impact(rows: object) -> tuple[Decimal | None, Decimal | None]:
    if not isinstance(rows, list):
        return None, None
    by_name = {row.get("N"): row for row in rows if isinstance(row, dict)}
    return _row_value(by_name.get("CURRENT_FUNDS")), _row_value(by_name.get("AFTER_FUNDS"))


def _row_value(row: object) -> Decimal | None:
    if not isinstance(row, dict):
        return None
    values = row.get("V")
    if isinstance(values, list):
        return _money(values[0]) if values else None
    return _money(values)


def _preview_warnings(data: dict) -> list[str]:
    raw = data.get("warns")
    if not isinstance(raw, list) or not raw:
        raw = [data[key] for key in ("warn", "error") if data.get(key)]
    return [cleaned for item in raw if item and (cleaned := _clean_warning(str(item)))]


def _clean_warning(text: str) -> str:
    text = _WARN_CODE_RE.sub("", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


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
