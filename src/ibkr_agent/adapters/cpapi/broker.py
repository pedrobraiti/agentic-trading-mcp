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
    BracketRequest,
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from .client import CpapiClient, CpapiError

# Benign warnings accepted by default — standard CPAPI precaution confirmations
# for our order types (MKT + cashQty, and LMT). The API itself marks them all as
# isSuppressible=true / "Accept and Continue". Mapped live on the real account:
#   o354   "order without market data" (no data subscription)
#   o163   limit price exceeds the percentage constraint vs. the market — expected
#          for a deliberate LIMIT order placed away from the current price
#   o10152 Stop Variant Order Confirmation (STOP / STOP_LIMIT / TRAIL — expected, we
#          place these on purpose)
#   o10164 Market Order Confirmation (market-order risk — we use MKT on purpose)
#   o10223 Confirm Mandatory Cap Price (IB may apply a protective cap/floor)
#   o10151 disclaimer: trader's responsibility over cash quantity details
#   o10153 Cash Quantity Order Confirmation (cashQty is simulated: cancels once the amount is spent)
DEFAULT_ACCEPTED_MESSAGE_IDS = frozenset(
    {"o354", "o163", "o10152", "o10164", "o10223", "o10151", "o10153"}
)

# A bracket submits 3 orders, each able to raise its own precaution — allow more rounds.
_MAX_REPLY_ROUNDS = 12

# The gateway can answer an order POST with a transient 503 *after the order already
# landed*. Retrying blindly would double-submit, so we first look the order up by its
# client id (cOID) and only retry when it genuinely didn't land.
_ORDER_POST_RETRIES = 2

# CPAPI order-status vocabulary: PendingSubmit, PreSubmitted, Submitted, PendingCancel,
# Cancelled, Filled, Inactive. Note "Rejected" is a TWS-API string CPAPI does NOT emit —
# a rejected/dead order arrives as "Inactive", so we must map it (else it falls to UNKNOWN
# and wait_for_fill polls to the timeout on an order that's already dead).
_STATUS_MAP = {
    "submitted": OrderStatus.SUBMITTED,
    "presubmitted": OrderStatus.PENDING,
    "pendingsubmit": OrderStatus.PENDING,
    "pendingcancel": OrderStatus.PENDING,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "apicancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "inactive": OrderStatus.INACTIVE,
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

        order = self._build_order(request, conid)
        response = await self._post_orders({"orders": [order]}, [order["cOID"]])
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

    async def place_bracket(self, bracket: BracketRequest) -> list[OrderResult]:
        entry = bracket.entry
        conid = await self._resolve_conid(entry.symbol)
        if conid is None:
            raise CpapiError(f"Could not resolve the conid for {entry.symbol}.")

        parent = self._build_order(entry, conid)
        exit_side = OrderSide.SELL if entry.side is OrderSide.BUY else OrderSide.BUY
        quantity = float(entry.quantity)
        # Both exits carry their trigger in `price` (LMT = limit, STP = stop trigger).
        take_profit = self._child_order(
            conid, exit_side, quantity, parent["cOID"], OrderType.LIMIT,
            price=float(bracket.take_profit_price),
        )
        stop_loss = self._child_order(
            conid, exit_side, quantity, parent["cOID"], OrderType.STOP,
            price=float(bracket.stop_loss_price),
        )

        legs = [
            ("entry", parent, entry.side),
            ("take_profit", take_profit, exit_side),
            ("stop_loss", stop_loss, exit_side),
        ]
        coids = [leg[1]["cOID"] for leg in legs]
        response = await self._post_orders({"orders": [parent, take_profit, stop_loss]}, coids)
        response = await self._resolve_replies(response)
        return _parse_bracket_acks(response, entry.symbol, legs)

    async def _post_orders(self, payload: dict, coids: list[str]) -> object:
        """POST orders, surviving a transient 503 without double-submitting.

        On 503 the order may already have landed, so before retrying we look it up by
        cOID (the live order's ``order_ref``); if it's there we return that instead of
        sending again. Only a genuine miss is retried.
        """
        url = f"/iserver/account/{self._account_id}/orders"
        for attempt in range(_ORDER_POST_RETRIES + 1):
            try:
                return await self._client.post(url, json=payload)
            except CpapiError as exc:
                if exc.status != 503 or attempt == _ORDER_POST_RETRIES:
                    raise
                # The 503 may have come AFTER the order landed. Look it up by cOID. If the
                # lookup itself fails we cannot tell whether it landed, so we must NOT
                # resend (that risks a duplicate) — surface an indeterminate error instead.
                try:
                    landed = await self._orders_by_coids(coids)
                except CpapiError as lookup_exc:
                    raise CpapiError(
                        "Order POST returned 503 and the follow-up lookup failed, so it is "
                        "UNKNOWN whether the order landed. Not resending (would risk a "
                        "duplicate). Check open_orders / trade_history before retrying."
                    ) from lookup_exc
                if landed:
                    return landed
                # Confirmed absent → safe to retry. NOTE (verify against a live gateway):
                # this "absent" verdict assumes a landed order is already visible in
                # /iserver/account/orders and that IBKR dedupes a resend by cOID/order_ref.
                # If a just-filled order can be missing from that snapshot, a resend here
                # could double-submit — the residual risk flagged in the ADR-013 audit.
        raise CpapiError("Order POST failed after retries.")  # pragma: no cover

    async def _orders_by_coids(self, coids: list[str]) -> list[dict]:
        """Live orders whose ``order_ref`` (the cOID we sent) is in ``coids``, as acks.

        Propagates CpapiError on failure (the caller must distinguish "confirmed absent"
        from "couldn't check" — only the former is safe to resend).
        """
        wanted = set(coids)
        # Warmup: the 1st call instantiates the orders endpoint, the 2nd returns data.
        await self._client.get("/iserver/account/orders")
        data = await self._client.get("/iserver/account/orders")
        orders = data.get("orders", []) if isinstance(data, dict) else []
        acks = []
        for order in orders:
            if isinstance(order, dict) and order.get("order_ref") in wanted:
                acks.append(
                    {"order_id": order.get("orderId"), "order_status": order.get("status"),
                     "local_order_id": order.get("order_ref")}
                )
        return acks

    def _child_order(
        self, conid: int, side: OrderSide, quantity: float, parent_coid: str,
        order_type: OrderType, *, price: float,
    ) -> dict:
        return {
            "conid": conid,
            "orderType": order_type.value,
            "side": side.value,
            "quantity": quantity,
            "price": price,
            "tif": "GTC",
            "cOID": self._id_factory(),
            "parentId": parent_coid,
        }

    async def get_order_status(self, order_id: str) -> OrderResult:
        data = await self._client.get(f"/iserver/account/order/status/{order_id}")
        return _order_status_to_result(data, order_id)

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
        # A cancel can come back as a dict ack, a list-wrapped ack, or (rarely) a
        # confirmation question. Normalize the list so we read the real status. We do NOT
        # route this through the order-warning allow-list: a cancel-confirmation id won't
        # be in it, and declining would refuse the cancel and raise — surprising a caller
        # pulling a protective stop. A question is reported as PENDING with its text instead.
        ack = response[0] if isinstance(response, list) and response else response
        data = ack if isinstance(ack, dict) else {}
        message = data.get("msg") or data.get("message")
        if isinstance(message, list):
            message = "; ".join(str(part) for part in message)
        if message is None and not data and response is not None:
            message = str(response)
        # IBKR's DELETE only ACKNOWLEDGES the cancel request — it is not confirmation the
        # order is gone (an already-filled order returns 200 with a "cannot be cancelled"
        # msg). Only report CANCELLED when the gateway explicitly says so; otherwise report
        # PENDING and let the caller confirm via order_status. Never claim a false cancel.
        raw_status = str(data.get("order_status") or data.get("status") or "").lower()
        if "cancel" in raw_status:
            status = OrderStatus.CANCELLED
        elif raw_status:
            status = _map_status(raw_status)
        else:
            status = OrderStatus.PENDING
        return OrderResult(
            order_id=order_id,
            status=status,
            symbol=symbol,
            side=side,
            message=message,
            raw=data or None,
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
        # CPAPI price-field convention (validated live):
        #   LMT        → price = limit
        #   STP        → price = stop trigger (NOT auxPrice)
        #   STOP_LIMIT → price = limit, auxPrice = stop trigger
        #   TRAIL      → trailingAmt + trailingType ("amt" $ / "%")
        if request.order_type is OrderType.LIMIT and request.limit_price:
            order["price"] = float(request.limit_price)
        elif request.order_type is OrderType.STOP and request.stop_price:
            order["price"] = float(request.stop_price)
        elif request.order_type is OrderType.STOP_LIMIT:
            order["price"] = float(request.limit_price)
            order["auxPrice"] = float(request.stop_price)
        elif request.order_type is OrderType.TRAIL and request.trailing_amount:
            order["trailingAmt"] = float(request.trailing_amount)
            order["trailingType"] = request.trailing_type.value
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

        # Ran out of rounds with a question still pending — decline it so we don't leave an
        # order half-confirmed, then report the abort.
        leftover = _as_question(response)
        if leftover is not None:
            await self._decline(leftover["id"])
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
    return OrderResult(
        order_id=str(order.get("orderId", "")),
        status=_map_status(order.get("status")),
        symbol=str(order.get("ticker", "")),
        side=_to_side(order.get("side")),
        filled_quantity=_dec(order.get("filledQuantity")),
        message=order.get("orderDesc"),
        raw=order,
    )


def _parse_bracket_acks(
    response: object, symbol: str, legs: list[tuple[str, dict, OrderSide]]
) -> list[OrderResult]:
    """Map each order ack back to its bracket leg (entry / take_profit / stop_loss).

    Acks carry ``local_order_id`` (the cOID we sent), so each one is matched to the
    leg that produced it; the leg name is surfaced in ``message`` so the agent can
    tell the entry from its two exits.
    """
    acks = response if isinstance(response, list) else [response]
    by_coid = {leg[1]["cOID"]: leg for leg in legs}
    results: list[OrderResult] = []
    for ack in acks:
        if not isinstance(ack, dict):
            continue
        role, _order, side = by_coid.get(str(ack.get("local_order_id") or ""), (None, None, None))
        results.append(
            OrderResult(
                order_id=str(ack.get("order_id") or ""),
                status=_map_status(ack.get("order_status")),
                symbol=symbol.upper(),
                side=side or _to_side(ack.get("side")),
                message=role or ack.get("text"),
                raw=ack,
            )
        )
    return results


def _order_status_to_result(response: object, order_id: str) -> OrderResult:
    """Parse ``/iserver/account/order/status/{id}`` defensively; ``raw`` carries it all."""
    data = response if isinstance(response, dict) else {}
    return OrderResult(
        order_id=str(data.get("order_id") or order_id),
        status=_map_status(data.get("order_status") or data.get("status")),
        symbol=str(data.get("ticker") or data.get("symbol") or ""),
        side=_to_side(data.get("side")),
        filled_quantity=_dec(data.get("cum_fill") or data.get("filled_quantity")),
        avg_price=_dec(data.get("average_price") or data.get("avg_price")),
        message=data.get("order_status") or data.get("status"),
        raw=data or None,
    )


def _to_side(value: object) -> OrderSide:
    return OrderSide.BUY if str(value or "").upper() in ("BUY", "B") else OrderSide.SELL


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
