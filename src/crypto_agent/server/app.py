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
from trading_core.posture import add_trading_posture
from trading_core.reconcile import reconcile_pending as _reconcile

from ..config import daily_cap_off_while_live
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
    derives from the broker's real `isPaper`), here `account_type` only echoes the configured
    `CRYPTO_TRADING_MODE`; it is NOT independently verified against the exchange. That is
    surfaced as `identity_verified: false`, and because of it a real (non-dry-run) order
    requires the explicit `CRYPTO_ALLOW_LIVE` ack — so a sandbox label hiding live keys can't
    move real money on the dry-run flag alone.

    Posture (so an unattended caller can self-gate): `dry_run`, `allowlist_active`,
    `daily_cap_configured`, `remaining_daily_budget`, `unresolved_orders`, `trade_stops` and an
    advisory `safe_to_trade`. When live trading is armed with no cap, a `daily_cap_warning`.
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
        elif info.get("identity_verified") is False:
            status["identity_warning"] = (
                "PAPER is a config label, NOT venue-verified — live keys under a sandbox label "
                "would look like this. A real order requires CRYPTO_ALLOW_LIVE=true."
            )
        add_trading_posture(
            status, journal=svc.journal, max_daily_value=svc.settings.max_daily_value,
            dry_run=svc.settings.crypto_dry_run,
            allowlist_active=bool(svc.settings.symbol_allowlist.strip()),
        )
        if daily_cap_off_while_live(svc.settings):
            status["daily_cap_warning"] = (
                "No daily spend cap set (MAX_DAILY_VALUE) - only the per-order cap applies."
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
async def reconcile_pending(resolve_missing: bool = False) -> dict:
    """Reconcile dispatched-but-unconfirmed orders against the exchange's open orders.

    After a timeout/crash an order may have landed without its outcome journaled, so the
    safety layer BLOCKS an identical resend until it is reconciled. This clears that block:
    orders found resting on the exchange are marked resolved; ones not found stay blocked (they
    may have filled — resending blind would double them). Set `resolve_missing=true` to also
    clear the not-found ones AFTER verifying via positions/trade_history that they didn't fill.
    """
    svc = services()
    try:
        return _ok(await _reconcile(svc.broker, svc.journal, resolve_missing=resolve_missing))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


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
