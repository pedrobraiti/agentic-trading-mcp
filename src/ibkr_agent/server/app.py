"""Servidor MCP — expõe as capacidades de trading como tools para o agente.

Cada tool garante a sessão (``ensure_session``) antes de operar e devolve um dict
JSON-serializável. Erros de domínio/segurança viram ``{"ok": false, "error": ...}``
para o agente ler em vez de quebrar.
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
    """Status da sessão com o gateway da IBKR (authenticated/connected/competing)."""
    try:
        return _ok(await services().auth.status())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def market_status() -> dict:
    """Indica se o mercado dos EUA está aberto (RTH) agora."""
    return _ok({"market_open": services().market_is_open()})


@mcp.tool()
async def get_quote(symbol: str) -> dict:
    """Cotação atual (last/bid/ask) de um símbolo de ação dos EUA."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        quote = await svc.market_data.get_quote(symbol)
        return _ok(quote.model_dump(mode="json") if quote else None)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def account_summary() -> dict:
    """Resumo da conta: fundos disponíveis, net liquidation, buying power."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        summary = await svc.market_data.get_account_summary()
        return _ok(summary.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def positions() -> dict:
    """Posições abertas na conta."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        rows = await svc.market_data.get_positions()
        return _ok([p.model_dump(mode="json") for p in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def buy(
    symbol: str, cash_amount: float | None = None, quantity: int | None = None
) -> dict:
    """Compra a mercado. Informe `cash_amount` (US$, fracionário) OU `quantity` (ações inteiras)."""
    return await _place(OrderSide.BUY, symbol, cash_amount, quantity)


@mcp.tool()
async def sell(
    symbol: str, cash_amount: float | None = None, quantity: int | None = None
) -> dict:
    """Vende a mercado. Informe `cash_amount` (US$, fracionário) OU `quantity` (ações inteiras)."""
    return await _place(OrderSide.SELL, symbol, cash_amount, quantity)


@mcp.tool()
async def cancel_order(order_id: str) -> dict:
    """Cancela uma ordem aberta pelo seu order_id."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        result = await svc.broker.cancel_order(order_id)
        return _ok(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def open_orders() -> dict:
    """Lista as ordens ativas (live orders) na conta."""
    svc = services()
    try:
        await svc.auth.ensure_session()
        rows = await svc.broker.get_live_orders()
        return _ok([o.model_dump(mode="json") for o in rows])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def _place(
    side: OrderSide, symbol: str, cash_amount: float | None, quantity: int | None
) -> dict:
    svc = services()
    try:
        request = OrderRequest(
            symbol=symbol,
            side=side,
            cash_qty=Decimal(str(cash_amount)) if cash_amount is not None else None,
            quantity=quantity,
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
