"""Reconcile dispatched-but-unconfirmed orders against the venue's live orders.

After a timeout/crash an order may have landed without its outcome being journaled, so the
idempotency guard blocks an identical resend (fail-safe). This is how that block is cleared:
match each unresolved intent's ``client_order_id`` (cOID) to a live order on the venue and
mark it resolved. An order that is NOT found among the open orders is left blocked by default
(it may have filled and closed — resending blind would double it); the operator can accept the
risk explicitly with ``resolve_missing=True`` after verifying fills/positions.
"""

from __future__ import annotations

from typing import Protocol


class _BrokerLike(Protocol):
    async def get_live_orders(self) -> list: ...


def _live_ref(order: object) -> str | None:
    """The client order id (cOID) a live order was submitted with, across venues.

    IBKR carries it as ``order_ref``; CCXT as ``clientOrderId`` (sometimes only under
    ``info``). Returns None when the venue doesn't echo it back.
    """
    raw = getattr(order, "raw", None) or {}
    if not isinstance(raw, dict):
        return None
    ref = raw.get("order_ref") or raw.get("clientOrderId")
    if not ref:
        info = raw.get("info")
        if isinstance(info, dict):
            ref = info.get("clientOrderId") or info.get("clOrdId")
    return str(ref) if ref else None


async def reconcile_pending(broker: _BrokerLike, journal, *, resolve_missing: bool = False) -> dict:
    """Resolve in-flight orders against the venue. Returns a structured report.

    ``resolve_missing=False`` (default, fail-safe): only orders found resting on the venue are
    resolved; ones absent stay blocked and are reported so the operator can investigate.
    ``resolve_missing=True``: also clear the absent ones (an explicit operator acknowledgement
    that they've been verified — they did not fill, or were already handled).
    """
    pending = journal.unresolved_dispatches()
    report: dict = {"unresolved_before": len(pending), "resolved": [], "still_unresolved": []}
    if not pending:
        return report

    try:
        live = await broker.get_live_orders()
    except Exception as exc:  # noqa: BLE001 - can't reach the venue; nothing is resolved
        report["error"] = f"Could not read open orders to reconcile: {exc}"
        report["still_unresolved"] = [_summary(i) for i in pending]
        return report

    live_by_ref = {ref: o for o in live if (ref := _live_ref(o))}
    for intent in pending:
        coid = intent.get("client_order_id")
        found = live_by_ref.get(coid)
        if found is not None:
            journal.mark_resolved(
                coid, status=found.status.value, order_id=found.order_id,
                message="reconciled: found resting on the venue",
            )
            report["resolved"].append({**_summary(intent), "status": found.status.value,
                                        "order_id": found.order_id})
        elif resolve_missing:
            journal.mark_resolved(
                coid, status="unknown",
                message="reconciled: not among open orders; operator-accepted as resolved",
            )
            report["resolved"].append({**_summary(intent), "status": "unknown"})
        else:
            report["still_unresolved"].append(_summary(intent))
    return report


def _summary(intent: dict) -> dict:
    return {
        "client_order_id": intent.get("client_order_id"),
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "size": intent.get("cash_qty") or intent.get("quantity"),
    }
