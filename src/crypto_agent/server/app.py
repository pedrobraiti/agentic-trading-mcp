"""MCP server — exposes the crypto execution capabilities as tools for the agent.

Mirrors the IBKR server's tool names so the future orchestrating skill can treat both
venues uniformly. Every tool returns ``{"ok": bool, "data"/"error": ...}``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP

from trading_core.domain.models import OrderRequest, OrderSide, OrderType

from .services import Services, build_services

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """No session keeper is needed (API keys don't expire like the IBKR gateway); we only
    make sure the CCXT aiohttp session is closed on shutdown."""
    try:
        yield
    finally:
        if _services is not None:
            with contextlib.suppress(Exception):
                await _services.client.aclose()


mcp = FastMCP("agentic-trading-crypto", lifespan=_lifespan)


# A crypto exchange's balance is a fresh REST read, but a just-sent close still takes a
# moment to settle; reserve the symbol so a second close can't sell the same balance twice.
_CLOSE_COOLDOWN_SECONDS = 30.0
_recent_closes: dict[str, float] = {}
_INFLIGHT = float("inf")


def _evict_stale_closes(now: float) -> None:
    for symbol in [s for s, ts in _recent_closes.items() if now - ts >= _CLOSE_COOLDOWN_SECONDS]:
        _recent_closes.pop(symbol, None)


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


@mcp.tool()
async def session_status() -> dict:
    """Whether the API keys authenticate, plus which environment is live.

    Returns `authenticated`, `exchange`, `mode` (sandbox/live), `account_type`
    ("PAPER"/"LIVE") and — when live — a `warning`. Unlike IBKR (where `account_type`
    derives from the broker's real `isPaper`), here `account_type` only echoes the
    configured `CRYPTO_TRADING_MODE`; it is NOT independently verified against the
    exchange. Real-money protection rests on the `CRYPTO_ALLOW_LIVE` gate, not on this
    field. There is no gateway/login here — either the keys authenticate or they don't.
    """
    svc = services()
    try:
        info = await svc.account_info()
        status = {
            "authenticated": True,
            "exchange": svc.settings.crypto_exchange,
            "mode": svc.settings.crypto_trading_mode.value,
            **info,
        }
        if info.get("is_paper") is False:
            status["warning"] = (
                "LIVE crypto account — orders placed here move REAL money. "
                "Confirm symbol, side and amount with the user before sending."
            )
        return _ok(status)
    except Exception as exc:  # noqa: BLE001
        return _ok(
            {
                "authenticated": False,
                "exchange": svc.settings.crypto_exchange,
                "mode": svc.settings.crypto_trading_mode.value,
                "error": str(exc),
            }
        )


@mcp.tool()
async def market_status() -> dict:
    """Crypto trades 24/7, so the market is always open."""
    return _ok({"market_open": True})


@mcp.tool()
async def get_quote(symbol: str) -> dict:
    """Current quote (last/bid/ask) for a crypto pair (e.g. "BTC" → BTC/USDT, or "ETH/USDT")."""
    svc = services()
    try:
        quote = await svc.market_data.get_quote(symbol)
        return _ok(quote.model_dump(mode="json") if quote else None)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def get_quotes(symbols: list[str]) -> dict:
    """Quotes for several crypto pairs at once (one snapshot — cheaper for a watchlist)."""
    svc = services()
    try:
        quotes = await svc.market_data.get_quotes(symbols)
        return _ok([q.model_dump(mode="json") for q in quotes])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def account_summary() -> dict:
    """Account summary: free balance in the quote currency (e.g. USDT)."""
    svc = services()
    try:
        summary = await svc.market_data.get_account_summary()
        return _ok(summary.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def positions() -> dict:
    """Open positions: non-zero balances of base assets (spot), excluding the quote currency."""
    svc = services()
    try:
        rows = await svc.market_data.get_positions()
        return _ok([p.model_dump(mode="json") for p in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def portfolio() -> dict:
    """Combined snapshot: account summary + open positions (base-asset balances)."""
    svc = services()
    try:
        summary = await svc.market_data.get_account_summary()
        rows = await svc.market_data.get_positions()
        return _ok(
            {
                "account_type": "PAPER" if svc.settings.is_sandbox else "LIVE",
                "summary": summary.model_dump(mode="json"),
                "positions": [p.model_dump(mode="json") for p in rows],
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
    """Buy. Provide `cash_amount` (quote currency, e.g. USDT — fractional) OR `quantity` (base).

    Omit `limit_price` for a market order; pass it for a LIMIT (LIMIT requires `quantity`).
    Orders below the pair's minimum notional/amount are rejected with a clear message.
    """
    if limit_price is not None and cash_amount is not None:
        return _err(ValueError("LIMIT orders use 'quantity', not 'cash_amount'."))
    return await _place(OrderSide.BUY, symbol, cash_amount, quantity, limit_price)


@mcp.tool()
async def sell(symbol: str, quantity: float, limit_price: float | None = None) -> dict:
    """Sell by `quantity` (base asset). Omit `limit_price` for market, pass it for LIMIT.

    Selling by quote-currency value isn't supported; to exit 100% use `close_position`.
    """
    return await _place(OrderSide.SELL, symbol, None, quantity, limit_price)


@mcp.tool()
async def close_position(symbol: str) -> dict:
    """Closes 100% of a base asset's position by selling its full balance."""
    svc = services()
    try:
        normalized = svc.client.normalize_symbol(symbol)
        now = time.monotonic()
        _evict_stale_closes(now)
        sent_at = _recent_closes.get(normalized)
        if sent_at is not None and (now - sent_at) < _CLOSE_COOLDOWN_SECONDS:
            return _ok(
                {
                    "closed": False,
                    "reason": (
                        f"A close for {normalized} was just dispatched; confirm it via "
                        "positions/open_orders before closing again to avoid selling twice."
                    ),
                }
            )
        _recent_closes[normalized] = _INFLIGHT
        order_attempted = False
        try:
            held = await svc.market_data.held_quantity(symbol)
            if held is None or held <= 0:
                _recent_closes.pop(normalized, None)
                return _ok(
                    {"closed": False, "reason": f"No open position in {normalized}."}
                )
            request = OrderRequest(symbol=normalized, side=OrderSide.SELL, quantity=held)
            order_attempted = True
            result = await svc.broker.place_order(request)
            if result.dry_run or result.status.value in ("rejected", "cancelled"):
                _recent_closes.pop(normalized, None)
            else:
                _recent_closes[normalized] = time.monotonic()
            return _ok(result.model_dump(mode="json"))
        finally:
            if _recent_closes.get(normalized) is _INFLIGHT:
                if order_attempted:
                    _recent_closes[normalized] = time.monotonic()
                else:
                    _recent_closes.pop(normalized, None)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def order_status(order_id: str) -> dict:
    """Status of a placed order by its id (state, filled quantity, average price)."""
    svc = services()
    try:
        result = await svc.broker.get_order_status(order_id)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def cancel_order(order_id: str) -> dict:
    """Cancels an open order by its id."""
    svc = services()
    try:
        result = await svc.broker.cancel_order(order_id)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def open_orders() -> dict:
    """Lists the active (open) orders on the exchange."""
    svc = services()
    try:
        rows = await svc.broker.get_live_orders()
        return _ok([o.model_dump(mode="json") for o in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def trade_history(limit: int = 50) -> dict:
    """Local audit log of the agent's recent order attempts (buys, sells, dry-runs, blocks)."""
    try:
        return _ok(services().journal.read(limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def _place(
    side: OrderSide,
    symbol: str,
    cash_amount: float | None,
    quantity: float | None,
    limit_price: float | None,
) -> dict:
    svc = services()
    try:
        request = OrderRequest(
            symbol=svc.client.normalize_symbol(symbol),
            side=side,
            order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
            cash_qty=Decimal(str(cash_amount)) if cash_amount is not None else None,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
        )
        result = await svc.broker.place_order(request)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
