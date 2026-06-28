"""Build the "am I safe to trade right now?" posture appended to ``session_status``.

Purely informational/additive — it never blocks an order (the guards do that). It lets an
unattended caller self-gate: how much daily budget is left, whether it is in dry-run, whether
an allow-list is narrowing the universe, which in-flight orders are unresolved, and the active
reasons NOT to trade (a competing session, unresolved orders). Shared by both venues so the
two ``session_status`` tools report the same shape.
"""

from __future__ import annotations

from decimal import Decimal

from .journal import TradeJournal


def add_trading_posture(
    status: dict,
    *,
    journal: TradeJournal,
    max_daily_value: Decimal | None,
    dry_run: bool,
    allowlist_active: bool,
) -> dict:
    status["dry_run"] = dry_run
    status["allowlist_active"] = allowlist_active
    status["daily_cap_configured"] = max_daily_value is not None
    status["remaining_daily_budget"] = (
        str(max_daily_value - journal.spent_today()) if max_daily_value is not None else None
    )

    unresolved = journal.unresolved_dispatches()
    status["unresolved_orders"] = [
        {
            "client_order_id": row.get("client_order_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "size": row.get("cash_qty") or row.get("quantity"),
        }
        for row in unresolved
    ]

    stops = list(status.get("trade_stops", []))
    if status.get("competing"):
        stops.append(
            "another session is competing for this connection — it may steal the session "
            "mid-order; do not trade until it is sole"
        )
    if unresolved:
        stops.append(
            f"{len(unresolved)} dispatched order(s) unconfirmed — run reconcile_pending "
            "before sending more, or a resend could duplicate a live order"
        )
    status["trade_stops"] = stops
    # Advisory only: authenticated AND no active stop condition. The hard guards still apply.
    status["safe_to_trade"] = bool(status.get("authenticated", False)) and not stops
    return status
