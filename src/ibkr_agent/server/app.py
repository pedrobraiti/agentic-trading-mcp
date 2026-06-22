"""MCP server — exposes the trading capabilities as tools for the agent.

Each tool ensures the session (``ensure_session``) before operating and returns a
JSON-serializable dict. Domain/safety errors become ``{"ok": false, "error": ...}``
for the agent to read instead of breaking.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..domain.models import OrderRequest, OrderSide
from .services import Services, build_services

mcp = FastMCP("mcp-ibkr-agent")

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


@mcp.tool()
async def session_status() -> dict:
    """Status of the session with the IBKR gateway (authenticated/connected/competing)."""
    try:
        return _ok(await services().auth.status())
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
async def buy(
    symbol: str, cash_amount: float | None = None, quantity: float | None = None
) -> dict:
    """Market buy. Provide `cash_amount` (US$, fractional via cashQty) OR
    `quantity` (shares, fractional ok)."""
    return await _place(OrderSide.BUY, symbol, cash_amount, quantity)


@mcp.tool()
async def sell(symbol: str, quantity: float) -> dict:
    """Market sell by `quantity` (shares, fractional ok).

    IBKR does NOT accept selling by US$ value (cashQty is buy-only). To exit
    100% of a position use `close_position`; to sell a dollar amount,
    compute the quantity via `get_quote`.
    """
    return await _place(OrderSide.SELL, symbol, None, quantity)


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
        await svc.market_data.invalidate_positions()
        rows = await svc.market_data.get_positions()
        position = next((p for p in rows if p.conid == conid), None)
        if position is None or position.quantity == 0:
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
        result = await svc.broker.place_order(request)
        return _ok(result.model_dump(mode="json"))
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
    side: OrderSide, symbol: str, cash_amount: float | None, quantity: float | None
) -> dict:
    svc = services()
    try:
        request = OrderRequest(
            symbol=symbol,
            side=side,
            cash_qty=Decimal(str(cash_amount)) if cash_amount is not None else None,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
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
