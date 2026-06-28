"""Append-only trade journal (audit log) — every order attempt and its outcome.

Persisted as JSONL so it is greppable and dependency-free. The path is local and
gitignored; trade data never goes into the repo. This is what answers the question
"what did my agent actually do?", and it backs the daily-spend limit, the
duplicate-order guard and the persistent idempotency guard (across processes).

Each line is one record with a ``kind``:
  * ``intent``     — written BEFORE the order is dispatched (the crash-window guard), so a
                     crash between dispatch and the outcome can't hide a live order.
  * ``outcome``    — the result of the dispatch (filled/submitted/error/...). Default for
                     legacy records that predate ``kind``.
  * ``resolution`` — the fate of a previously-unconfirmed dispatch, confirmed by
                     reconciliation against the venue's open orders (clears the idempotency
                     block).

Records carry a ``client_order_id`` (the venue's cOID) so an intent, its outcome and its
resolution can be tied together across separate processes.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain.models import OrderRequest, OrderResult, OrderSide, TradingMode

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Statuses that prove the order's fate is settled (so it is no longer "in flight").
_TERMINAL_STATUSES = frozenset({"filled", "cancelled", "rejected", "inactive", "submitted"})
# An order moved no money / never lived, so it neither counts toward spend nor blocks a retry.
_NO_MONEY_STATUSES = frozenset({"rejected", "cancelled"})


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _request_size(request: OrderRequest) -> str:
    return str(request.cash_qty if request.cash_qty is not None else request.quantity)


def _request_fingerprint(request: OrderRequest) -> tuple[str, str, str, str]:
    """Identity of an order *intent* — symbol/side/type/size. A retry of the same intent
    produces the same fingerprint (even though it gets a fresh cOID), which is what lets the
    idempotency guard catch a resend of a dispatched-but-unconfirmed order."""
    return (
        request.symbol.upper(),
        request.side.value,
        request.order_type.value,
        _request_size(request),
    )


def _entry_fingerprint(entry: dict) -> tuple[str, str, str, str]:
    size = entry.get("cash_qty") or entry.get("quantity")
    return (entry.get("symbol"), entry.get("side"), entry.get("order_type"), str(size))


class TradeJournal:
    """Appends one JSONL record per order event and reads them back."""

    def __init__(self, path: str | Path, *, market_timezone: str = "America/New_York"):
        self._path = _resolve(path)
        self._tz = ZoneInfo(market_timezone)

    def record(
        self,
        *,
        request: OrderRequest,
        mode: TradingMode,
        dry_run: bool,
        notional: Decimal | None,
        result: OrderResult | None = None,
        error: Exception | None = None,
        sent: bool = False,
        client_order_id: str | None = None,
        kind: str = "outcome",
        status: str | None = None,
    ) -> dict:
        entry = {
            "timestamp": datetime.now(self._tz).isoformat(),
            "kind": kind,
            "client_order_id": client_order_id
            or (request.client_order_id if request is not None else None),
            "mode": str(mode),
            "symbol": request.symbol.upper(),
            "side": request.side.value,
            "order_type": request.order_type.value,
            "cash_qty": str(request.cash_qty) if request.cash_qty is not None else None,
            "quantity": str(request.quantity) if request.quantity is not None else None,
            "notional": str(notional) if notional is not None else None,
            "dry_run": result.dry_run if result is not None else dry_run,
            # `sent` = dispatched to the broker (it may have filled even if no order_id
            # came back, e.g. a timeout/503). The duplicate/idempotency guards key off this so
            # a retry of a sent-but-unconfirmed order is still caught.
            "sent": bool(sent) or (result is not None and result.order_id is not None),
            "order_id": result.order_id if result is not None else None,
            "status": status
            or (result.status.value if result is not None else "error"),
            "message": (result.message if result is not None else None)
            or (str(error) if error is not None else None),
        }
        self._append(entry)
        return entry

    def record_intent(
        self,
        *,
        request: OrderRequest,
        mode: TradingMode,
        notional: Decimal | None,
        client_order_id: str,
    ) -> dict:
        """Persist the INTENT to dispatch BEFORE calling the broker (the crash-window guard).

        Marked ``sent`` (we are about to put it on the wire) with no ``order_id`` and a
        ``pending`` status, so until a matching outcome/resolution lands it reads as an
        unresolved in-flight order. Counts toward today's spend (deduped by cOID against its
        own outcome) so a crash between dispatch and the outcome record can't hide money that
        may have moved.
        """
        return self.record(
            request=request,
            mode=mode,
            dry_run=False,
            notional=notional,
            sent=True,
            client_order_id=client_order_id,
            kind="intent",
            status="pending",
        )

    def mark_resolved(
        self, client_order_id: str, *, status: str, order_id: str | None = None,
        message: str | None = None,
    ) -> dict:
        """Record that a previously-unconfirmed dispatch's fate is now known (reconciled).

        Clears the persistent idempotency block for that cOID. ``status`` is the confirmed
        venue status (or ``unknown`` when the operator explicitly accepts an unresolvable one).
        """
        entry = {
            "timestamp": datetime.now(self._tz).isoformat(),
            "kind": "resolution",
            "client_order_id": client_order_id,
            "order_id": order_id,
            "status": status,
            "message": message,
        }
        self._append(entry)
        return entry

    def read(self, limit: int = 50) -> list[dict]:
        if not self._path.exists():
            return []
        entries: list[dict] = []
        corrupt = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # One bad line must not brick reads — the daily-spend cap and the
                # duplicate guard depend on this, so skip it and surface the damage.
                corrupt += 1
        if corrupt:
            logger.warning(
                "Skipped %d corrupt line(s) in the trade journal %s", corrupt, self._path
            )
        return entries[-limit:] if limit else entries

    def spent_today(self) -> Decimal:
        """Sum of today's BUY notionals that may have spent money (market-tz).

        Counts a buy that was acked (``order_id``) OR merely dispatched (``sent``) — the
        latter may have filled even without an ack (timeout/503), so for a spend cap we
        must assume it did. Excludes buys the gateway resolved as rejected/cancelled, which
        moved no money. An ``intent`` and its ``outcome`` share a cOID and are counted ONCE
        (deduped), so journaling the intent before dispatch never double-counts spend.
        """
        today = datetime.now(self._tz).date().isoformat()
        groups: dict[str, list[dict]] = defaultdict(list)
        loose: list[dict] = []
        resolutions: dict[str, str] = {}  # cOID -> terminal status from a reconcile resolution
        for entry in self.read(limit=0):
            if entry.get("kind") == "resolution":
                coid = entry.get("client_order_id")
                status = entry.get("status")
                if coid and status:
                    resolutions[coid] = status
                continue
            if entry.get("side") != OrderSide.BUY.value or not entry.get("notional"):
                continue
            if not str(entry.get("timestamp", "")).startswith(today):
                continue
            coid = entry.get("client_order_id")
            if coid:
                groups[coid].append(entry)
            else:
                loose.append(entry)

        total = Decimal(0)
        # A cOID group may carry a reconcile resolution telling us the order's terminal fate;
        # a loose (cOID-less) legacy record never does.
        evaluated: list[tuple[list[dict], str | None]] = [
            (records, resolutions.get(coid)) for coid, records in groups.items()
        ] + [([r], None) for r in loose]
        for records, resolution_status in evaluated:
            if not any(r.get("order_id") or r.get("sent") for r in records):
                continue
            # Only rejected/cancelled move no money — NOT `inactive`. CPAPI emits `inactive`
            # for BOTH a dead/rejected order AND one parked until the open, and there is no
            # reliable sub-reason offline to tell the two apart. So an `inactive` buy keeps
            # counting toward spend on purpose: the fail-safe direction is to assume money may
            # have moved (over-block, never over-spend). See ADR-015. A reconcile that DID
            # confirm the order cancelled/rejected frees the budget — it moved no money.
            statuses = [r.get("status") for r in records]
            if resolution_status is not None:
                statuses.append(resolution_status)
            if any(s in _NO_MONEY_STATUSES for s in statuses):
                continue
            notional = next((r.get("notional") for r in records if r.get("notional")), None)
            if notional is None:
                continue
            try:
                total += Decimal(notional)
            except (InvalidOperation, ValueError):
                pass
        return total

    def has_recent_duplicate(self, request: OrderRequest, window_seconds: float) -> bool:
        """True if an identical order (symbol/side/type/size) was placed within the window."""
        if window_seconds <= 0:
            return False
        cutoff = datetime.now(self._tz) - timedelta(seconds=window_seconds)
        fingerprint = _request_fingerprint(request)
        for entry in reversed(self.read(limit=200)):
            if entry.get("kind") == "resolution":
                continue
            # An order counts as a possible duplicate if it was dispatched to the broker
            # (`sent`) — even if no order_id came back (timeout/503), because it may have
            # filled. Pure guard-blocked attempts (never sent) don't count, and neither do
            # ones the gateway resolved as rejected/cancelled (nothing happened, so a
            # corrected retry must be allowed).
            if not (entry.get("sent") or entry.get("order_id")):
                continue
            if entry.get("status") in _NO_MONEY_STATUSES:
                continue
            if _entry_fingerprint(entry) == fingerprint:
                try:
                    when = datetime.fromisoformat(entry["timestamp"])
                except (ValueError, KeyError):
                    continue
                if when >= cutoff:
                    return True
        return False

    def has_unresolved_dispatch(self, request: OrderRequest) -> bool:
        """True if an identical intent has a dispatched-but-UNCONFIRMED order still in flight.

        This is the persistent idempotency guard, and it is NOT time-bounded (unlike the
        duplicate window): a buy that timed out / crashed mid-flight stays blocked until its
        fate is reconciled, however long the retry loop's backoff is. An order is "unresolved"
        when it was ``sent`` but never produced an ``order_id`` and no terminal status or
        ``resolution`` arrived for its cOID. A confirmed order (got an order_id) is NOT
        unresolved — re-buying the same thing later is a deliberate choice, gated only by the
        short duplicate window.
        """
        fingerprint = _request_fingerprint(request)
        return any(
            fp == fingerprint for fp, _ in self._unresolved_dispatches()
        )

    def unresolved_dispatches(self) -> list[dict]:
        """The intent/outcome records of every dispatched-but-unconfirmed order (for reconcile).

        One representative record per unresolved cOID, carrying ``client_order_id``, symbol,
        side and size so the caller can look it up among the venue's open orders.
        """
        return [record for _, record in self._unresolved_dispatches()]

    def _unresolved_dispatches(self) -> list[tuple[tuple[str, str, str, str], dict]]:
        by_coid: dict[str, list[dict]] = defaultdict(list)
        for entry in self.read(limit=0):
            coid = entry.get("client_order_id")
            if coid:
                by_coid[coid].append(entry)
        unresolved: list[tuple[tuple[str, str, str, str], dict]] = []
        for records in by_coid.values():
            if any(r.get("dry_run") for r in records):
                continue
            if not any(r.get("sent") for r in records):
                continue
            # Resolved once any record carries an order_id (acked), a terminal status, or an
            # explicit resolution. Until then the dispatch's fate is unknown → still in flight.
            resolved = any(
                r.get("kind") == "resolution"
                or r.get("order_id")
                or r.get("status") in _TERMINAL_STATUSES
                for r in records
            )
            if resolved:
                continue
            representative = next((r for r in records if r.get("sent")), records[0])
            unresolved.append((_entry_fingerprint(representative), representative))
        return unresolved

    def _append(self, entry: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # Durable append: flush + fsync so a dispatched order's record survives a crash /
        # power loss right after it is written. A torn final line is already tolerated by
        # read() (it skips unparseable lines), so an interrupted write can't brick the journal.
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
