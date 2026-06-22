"""Append-only trade journal (audit log) — every order attempt and its outcome.

Persisted as JSONL so it is greppable and dependency-free. The path is local and
gitignored; trade data never goes into the repo. This is what answers the question
"what did my agent actually do?", and it backs the daily-spend limit and the
duplicate-order guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain.models import OrderRequest, OrderResult, OrderSide, TradingMode

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else _PROJECT_ROOT / path


class TradeJournal:
    """Appends one JSONL record per order attempt and reads them back."""

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
    ) -> dict:
        entry = {
            "timestamp": datetime.now(self._tz).isoformat(),
            "mode": str(mode),
            "symbol": request.symbol.upper(),
            "side": request.side.value,
            "order_type": request.order_type.value,
            "cash_qty": str(request.cash_qty) if request.cash_qty is not None else None,
            "quantity": str(request.quantity) if request.quantity is not None else None,
            "notional": str(notional) if notional is not None else None,
            "dry_run": result.dry_run if result is not None else dry_run,
            "order_id": result.order_id if result is not None else None,
            "status": result.status.value if result is not None else "error",
            "message": (result.message if result is not None else None)
            or (str(error) if error is not None else None),
        }
        self._append(entry)
        return entry

    def read(self, limit: int = 50) -> list[dict]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        return entries[-limit:] if limit else entries

    def spent_today(self) -> Decimal:
        """Sum of BUY notionals actually placed (got an order_id) today, market-tz."""
        today = datetime.now(self._tz).date().isoformat()
        total = Decimal(0)
        for entry in self.read(limit=0):
            if (
                entry.get("side") == OrderSide.BUY.value
                and entry.get("order_id")
                and entry.get("notional")
                and str(entry.get("timestamp", "")).startswith(today)
            ):
                try:
                    total += Decimal(entry["notional"])
                except (InvalidOperation, ValueError):
                    pass
        return total

    def has_recent_duplicate(self, request: OrderRequest, window_seconds: float) -> bool:
        """True if an identical order (symbol/side/size) was placed within the window."""
        if window_seconds <= 0:
            return False
        cutoff = datetime.now(self._tz) - timedelta(seconds=window_seconds)
        size = str(request.cash_qty if request.cash_qty is not None else request.quantity)
        for entry in reversed(self.read(limit=200)):
            if not entry.get("order_id"):
                continue  # only orders that were actually placed count as duplicates
            entry_size = entry.get("cash_qty") or entry.get("quantity")
            if (
                entry.get("symbol") == request.symbol.upper()
                and entry.get("side") == request.side.value
                and entry_size == size
            ):
                try:
                    when = datetime.fromisoformat(entry["timestamp"])
                except (ValueError, KeyError):
                    continue
                if when >= cutoff:
                    return True
        return False

    def _append(self, entry: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
