"""MCP server — exposes the trading capabilities as tools for the agent.

Each tool ensures the session (``ensure_session``) before operating and returns a
JSON-serializable dict. Domain/safety errors become ``{"ok": false, "error": ...}``
for the agent to read instead of breaking.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP

from trading_core.domain.models import (
    BracketRequest,
    OrderRequest,
    OrderSide,
    OrderType,
    TrailingType,
)
from trading_core.posture import add_trading_posture
from trading_core.reconcile import reconcile_pending as _reconcile

from ..config import daily_cap_off_while_live
from ..keepalive import _alert
from ..session import SessionKeeper
from .services import Services, build_services

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Run a background keep-alive so the brokerage session stays warm.

    Without this, a long-lived MCP process would silently lose its session
    (no ``/tickle``) and the next tool call would fail with an auth error. The
    keeper only tickles and alerts — it cannot log in for a retail account.
    """
    svc = services()
    keeper = SessionKeeper(
        svc.auth,
        interval_seconds=svc.settings.tickle_interval_seconds,
        on_alert=_alert,
    )
    task = asyncio.create_task(keeper.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await svc.client.aclose()


mcp = FastMCP("mcp-ibkr-agent", lifespan=_lifespan)


# IBKR's portfolio is eventually-consistent: after a close is sent, the position can still
# show its old size for tens of seconds. To stop a (sequential OR concurrent) retry from
# selling the same position twice (an unintended short), we reserve the contract before
# touching the network and refuse a second close within this cooldown, pointing the caller
# to order_status. Keyed by conid → monotonic time reserved. The reservation is released if
# nothing was actually dispatched (no position, dry-run, or a rejected/failed order).
_CLOSE_COOLDOWN_SECONDS = 45.0
_recent_closes: dict[int, float] = {}
_NON_DISPATCH_STATUSES = {"rejected", "inactive", "cancelled"}
# Sentinel timestamp for an in-flight close: `now - inf` is always < the cooldown (so a
# second close is refused) and never >= it (so eviction can't drop a still-running close).
_INFLIGHT = float("inf")


def _evict_stale_closes(now: float) -> None:
    for conid in [c for c, ts in _recent_closes.items() if now - ts >= _CLOSE_COOLDOWN_SECONDS]:
        _recent_closes.pop(conid, None)


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


@mcp.tool()
async def session_status() -> dict:
    """Trading posture — "am I safe to trade right now?" — plus which account is live.

    Identity: `authenticated`/`connected`/`competing`, `account_id`, `account_type`
    ("LIVE"/"PAPER", the ground truth from IBKR's `isPaper`, NOT the `IBKR_TRADING_MODE`
    label) and `is_paper`. A LIVE account includes a `warning`.

    Posture (so an unattended caller can self-gate): `dry_run`, `allowlist_active`,
    `daily_cap_configured`, `remaining_daily_budget` (when a cap is set), `unresolved_orders`
    (dispatched-but-unconfirmed orders that must be reconciled before resending), `trade_stops`
    (active reasons NOT to trade — a competing session, or unresolved in-flight orders) and an
    advisory `safe_to_trade`. When live trading is armed with no cap, a `daily_cap_warning`.
    """
    svc = services()
    try:
        status = await svc.auth.status()
        if status.get("authenticated"):
            info = await svc.auth.account_info()
            status.update(info)
            if info.get("is_paper") is False:
                status["warning"] = (
                    "LIVE account — orders placed here move REAL money. "
                    "Confirm symbol, side and amount with the user before sending."
                )
        add_trading_posture(
            status, journal=svc.journal, max_daily_value=svc.settings.max_daily_value,
            dry_run=svc.settings.trading_dry_run,
            allowlist_active=bool(svc.settings.symbol_allowlist.strip()),
        )
        if daily_cap_off_while_live(svc.settings):
            status["daily_cap_warning"] = (
                "No daily spend cap set (MAX_DAILY_VALUE) - only the per-order cap applies."
            )
        return _ok(status)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def reconcile_pending(resolve_missing: bool = False) -> dict:
    """Reconcile dispatched-but-unconfirmed orders against IBKR's open orders.

    After a timeout/crash an order may have landed without its outcome journaled, so the
    safety layer BLOCKS an identical resend until it is reconciled. This clears that block:
    orders found resting on IBKR are marked resolved; ones not found stay blocked (they may
    have filled — resending blind would double them). Set `resolve_missing=true` to also clear
    the not-found ones AFTER you've verified via positions/trade_history that they didn't fill.
    """
    svc = services()
    try:
        await svc.auth.ensure_session()
        return _ok(await _reconcile(svc.broker, svc.journal, resolve_missing=resolve_missing))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def market_status() -> dict:
    """Indicates whether the US market is open (RTH) right now."""
    return _ok({"market_open": services().market_is_open()})


@mcp.tool()
async def get_quote(symbol: str) -> dict:
    """Current quote (last/bid/ask) for a US stock symbol."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        quote = await svc.market_data.get_quote(symbol)
        return _ok(quote.model_dump(mode="json") if quote else None)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def get_quotes(symbols: list[str]) -> dict:
    """Quotes for several US stock symbols at once (one snapshot — cheaper for a watchlist)."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        quotes = await svc.market_data.get_quotes(symbols)
        return _ok([q.model_dump(mode="json") for q in quotes])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def account_summary() -> dict:
    """Account summary: available funds, net liquidation, buying power."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        summary = await svc.market_data.get_account_summary()
        return _ok(summary.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def positions() -> dict:
    """Open positions in the account."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        rows = await svc.market_data.get_positions()
        return _ok([p.model_dump(mode="json") for p in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def portfolio() -> dict:
    """Combined snapshot: account summary + open positions + total unrealized P&L."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        info = await svc.auth.account_info()
        summary = await svc.market_data.get_account_summary()
        rows = await svc.market_data.get_positions()
        total_pnl = sum((p.unrealized_pnl or Decimal(0) for p in rows), Decimal(0))
        return _ok(
            {
                "account_type": info.get("account_type"),
                "summary": summary.model_dump(mode="json"),
                "positions": [p.model_dump(mode="json") for p in rows],
                "unrealized_pnl": str(total_pnl),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def buy(
    symbol: str,
    cash_amount: float | None = None,
    quantity: float | None = None,
    limit_price: float | None = None,
) -> dict:
    """Buy. Provide `cash_amount` (US$, fractional via cashQty) OR `quantity` (shares).

    Omit `limit_price` for a market order. Pass `limit_price` for a LIMIT order —
    LIMIT requires `quantity` (cashQty is market-only). A limit order may surface a
    confirmation IBKR hasn't mapped yet; if so it is blocked (safe) until allow-listed.
    """
    return await _place(OrderSide.BUY, symbol, cash_amount, quantity, limit_price)


@mcp.tool()
async def sell(symbol: str, quantity: float, limit_price: float | None = None) -> dict:
    """Sell by `quantity` (shares, fractional ok). Omit `limit_price` for market, pass it for LIMIT.

    IBKR does NOT accept selling by US$ value (cashQty is buy-only). To exit
    100% of a position use `close_position`; to sell a dollar amount,
    compute the quantity via `get_quote`.
    """
    return await _place(OrderSide.SELL, symbol, None, quantity, limit_price)


@mcp.tool()
async def close_position(symbol: str) -> dict:
    """Closes 100% of a symbol's position, trading the exact fractional quantity.

    Reads the exact position size and sends the opposite order. Note: IBKR's
    portfolio is eventually-consistent — right after a recent BUY the position may
    not appear yet (and the close will return `closed=False`). In that case, wait
    a few seconds and try again, or sell the exact quantity via `sell`.
    """
    svc = services()
    try:
        await svc.auth.ensure_session()
        conid = await svc.market_data.resolve_conid(symbol)
        if conid is None:
            return _ok(
                {
                    "closed": False,
                    "reason": (
                        f"Could not resolve {symbol.upper()} to a tradable US contract — "
                        "cannot confirm or close a position. Check the symbol."
                    ),
                }
            )
        # Reserve the contract SYNCHRONOUSLY (no await between the check and the claim) so a
        # second close — sequential or concurrent — sees it and backs off.
        now = time.monotonic()
        _evict_stale_closes(now)
        sent_at = _recent_closes.get(conid)
        if sent_at is not None and (now - sent_at) < _CLOSE_COOLDOWN_SECONDS:
            return _ok(
                {
                    "closed": False,
                    "reason": (
                        f"A close for {symbol.upper()} was just dispatched and IBKR's "
                        "portfolio may still show the old size. Confirm the fill via "
                        "order_status / positions before closing again, to avoid selling "
                        "the position twice (an unintended short)."
                    ),
                }
            )
        # Mark it IN-FLIGHT with a sentinel that never evicts and always refuses, so a
        # staggered retry can't slip past even if the dispatch outlasts the cooldown.
        _recent_closes[conid] = _INFLIGHT
        order_attempted = False
        try:
            await svc.market_data.invalidate_positions()
            rows = await svc.market_data.get_positions()
            position = next((p for p in rows if p.conid == conid), None)
            if position is None or position.quantity == 0:
                _recent_closes.pop(conid, None)  # nothing was sent → release
                return _ok(
                    {
                        "closed": False,
                        "reason": (
                            f"No open position in {symbol.upper()} (remember: IBKR's "
                            "portfolio can take tens of seconds to reflect a recent buy)."
                        ),
                    }
                )

            side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            request = OrderRequest(symbol=symbol, side=side, quantity=abs(position.quantity))
            order_attempted = True
            result = await svc.broker.place_order(request)
            dispatched = not result.dry_run and result.status.value not in _NON_DISPATCH_STATUSES
            if dispatched:
                _recent_closes[conid] = time.monotonic()  # start the cooldown from completion
            else:
                _recent_closes.pop(conid, None)  # known no-dispatch (dry-run/rejected) → release
            return _ok(result.model_dump(mode="json"))
        finally:
            # If we left the in-flight sentinel in place, an exception OR a CancelledError
            # (a BaseException, so it bypasses `except Exception`) aborted us before we
            # normalized it. Normalize here for EVERY exit path so the sentinel can't leak
            # forever: hold the cooldown if the order may have landed, else release.
            if _recent_closes.get(conid) is _INFLIGHT:
                if order_attempted:
                    _recent_closes[conid] = time.monotonic()
                else:
                    _recent_closes.pop(conid, None)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def stop_order(
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
    limit_price: float | None = None,
) -> dict:
    """Place a STOP order (e.g. a stop-loss). Triggers a market order when `stop_price` is hit.

    `side` is "BUY" or "SELL" (a stop-loss on a long position is a SELL). Pass
    `limit_price` to make it a STOP-LIMIT (becomes a limit order on trigger instead
    of a market order). Sized by `quantity` (shares).
    """
    svc = services()
    try:
        order_type = OrderType.STOP_LIMIT if limit_price is not None else OrderType.STOP
        request = OrderRequest(
            symbol=symbol,
            side=OrderSide(side.upper()),
            order_type=order_type,
            quantity=Decimal(str(quantity)),
            stop_price=Decimal(str(stop_price)),
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
        )
        await svc.auth.ensure_session()
        result = await svc.broker.place_order(request)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def trailing_stop(
    symbol: str,
    side: str,
    quantity: float,
    trail_amount: float | None = None,
    trail_percent: float | None = None,
) -> dict:
    """Place a TRAILING stop — a stop that follows the price (locks in gains as it moves).

    The trigger trails the market by `trail_amount` (US$) **or** `trail_percent` (%).
    `side` is "BUY" or "SELL" (a trailing stop-loss on a long position is a SELL).
    Sized by `quantity` (shares).
    """
    svc = services()
    try:
        if (trail_amount is None) == (trail_percent is None):
            raise ValueError("Provide exactly one of 'trail_amount' and 'trail_percent'.")
        amount = trail_amount if trail_amount is not None else trail_percent
        trailing_type = TrailingType.AMOUNT if trail_amount is not None else TrailingType.PERCENT
        request = OrderRequest(
            symbol=symbol,
            side=OrderSide(side.upper()),
            order_type=OrderType.TRAIL,
            quantity=Decimal(str(quantity)),
            trailing_amount=Decimal(str(amount)),
            trailing_type=trailing_type,
        )
        await svc.auth.ensure_session()
        result = await svc.broker.place_order(request)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def bracket_order(
    symbol: str,
    quantity: float,
    take_profit: float,
    stop_loss: float,
    side: str = "BUY",
    entry_limit_price: float | None = None,
) -> dict:
    """Place an entry order with attached take-profit and stop-loss exits (OCO).

    The entry buys (or sells) `quantity` shares — market by default, or a limit if
    `entry_limit_price` is set. Once it fills, two exits go live: a limit at
    `take_profit` and a stop at `stop_loss`; when one fills the other is cancelled.
    Returns one result per leg (labelled entry / take_profit / stop_loss in `message`).
    """
    svc = services()
    try:
        entry_type = OrderType.LIMIT if entry_limit_price is not None else OrderType.MARKET
        entry = OrderRequest(
            symbol=symbol,
            side=OrderSide(side.upper()),
            order_type=entry_type,
            quantity=Decimal(str(quantity)),
            limit_price=Decimal(str(entry_limit_price)) if entry_limit_price is not None else None,
        )
        bracket = BracketRequest(
            entry=entry,
            take_profit_price=Decimal(str(take_profit)),
            stop_loss_price=Decimal(str(stop_loss)),
        )
        await svc.auth.ensure_session()
        results = await svc.broker.place_bracket(bracket)
        return _ok([r.model_dump(mode="json") for r in results])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def preview_order(
    symbol: str,
    side: str = "BUY",
    cash_amount: float | None = None,
    quantity: float | None = None,
    limit_price: float | None = None,
) -> dict:
    """Preview an order's impact (margin, estimated commission, warnings) WITHOUT sending it.

    Uses IBKR's whatif so the agent can reason about cost/margin before committing. `side`
    is "BUY" or "SELL"; size is `cash_amount` (USD) or `quantity` (shares). Pass
    `limit_price` to preview a LIMIT order (needs `quantity`).
    """
    svc = services()
    try:
        request = OrderRequest(
            symbol=symbol,
            side=OrderSide(side.upper()),
            order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
            cash_qty=Decimal(str(cash_amount)) if cash_amount is not None else None,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
        )
        await svc.auth.ensure_session()
        preview = await svc.broker.preview_order(request)
        return _ok(preview.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def order_status(order_id: str) -> dict:
    """Status of a placed order by its order_id: state, filled quantity, average price.

    Use this after `buy`/`sell` to confirm whether the order actually filled —
    `positions` is eventually-consistent and lags right after a trade.
    """
    svc = services()
    try:
        await svc.auth.ensure_session()
        result = await svc.broker.get_order_status(order_id)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


_TERMINAL_STATUSES = {"filled", "cancelled", "rejected", "inactive"}
_WAIT_POLL_SECONDS = 2.0
_WAIT_MAX_TIMEOUT = 120.0


@mcp.tool()
async def wait_for_fill(order_id: str, timeout_seconds: float = 30.0) -> dict:
    """Poll an order until it reaches a terminal state (filled/cancelled/rejected/inactive),
    or the timeout elapses.

    Closes the confirm-the-fill loop so the agent doesn't have to orchestrate the
    retry itself. Returns the latest status with `timed_out` true if it was still
    working when time ran out. `timeout_seconds` is capped at 120. Note: an `inactive`
    result usually means the order was rejected/killed, but IBKR also uses it for an
    order parked until the market opens — so confirm intent before assuming it's dead.
    """
    svc = services()
    try:
        await svc.auth.ensure_session()
        deadline = max(0.0, min(timeout_seconds, _WAIT_MAX_TIMEOUT))
        elapsed = 0.0
        result = await svc.broker.get_order_status(order_id)
        while result.status.value not in _TERMINAL_STATUSES and elapsed < deadline:
            await asyncio.sleep(_WAIT_POLL_SECONDS)
            elapsed += _WAIT_POLL_SECONDS
            result = await svc.broker.get_order_status(order_id)
        payload = result.model_dump(mode="json")
        payload["timed_out"] = result.status.value not in _TERMINAL_STATUSES
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def cancel_order(order_id: str) -> dict:
    """Cancels an open order by its order_id."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        result = await svc.broker.cancel_order(order_id)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def open_orders() -> dict:
    """Lists the active orders (live orders) in the account."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        rows = await svc.broker.get_live_orders()
        return _ok([o.model_dump(mode="json") for o in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def trade_history(limit: int = 50) -> dict:
    """Audit log of the agent's recent order attempts (buys, sells, dry-runs, blocks).

    Reads the local trade journal — answers "what did my agent do?". Does not hit IBKR.
    """
    try:
        return _ok(services().journal.read(limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def _place(
    side: OrderSide,
    symbol: str,
    cash_amount: float | None,
    quantity: float | None,
    limit_price: float | None = None,
) -> dict:
    svc = services()
    try:
        if limit_price is not None and cash_amount is not None:
            raise ValueError(
                "LIMIT orders use 'quantity', not 'cash_amount' (cashQty is market-only)."
            )
        request = OrderRequest(
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
            cash_qty=Decimal(str(cash_amount)) if cash_amount is not None else None,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
        )
        await svc.auth.ensure_session()
        result = await svc.broker.place_order(request)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
