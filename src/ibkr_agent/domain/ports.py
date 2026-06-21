"""Portas (interfaces) do domínio — contratos que os adapters concretos implementam.

São ``Protocol`` assíncronos: a implementação CPAPI (e futuros adapters como
ib_async para dados, ou OAuth para auth) deve satisfazê-los sem herança explícita.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    AccountSummary,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)


@runtime_checkable
class AuthPort(Protocol):
    """Gerencia a sessão/autenticação com o broker (Gateway hoje, OAuth no futuro)."""

    async def ensure_session(self) -> None:
        """Garante uma sessão válida, (re)autenticando ou fazendo keep-alive se preciso."""
        ...

    async def is_authenticated(self) -> bool:
        ...


@runtime_checkable
class MarketDataPort(Protocol):
    """Leitura de mercado e conta: resolução de símbolo, cotação, saldo e posições."""

    async def resolve_conid(self, symbol: str) -> int | None:
        ...

    async def get_quote(self, symbol: str) -> Quote | None:
        ...

    async def get_account_summary(self) -> AccountSummary:
        ...

    async def get_positions(self) -> list[Position]:
        ...


@runtime_checkable
class BrokerPort(Protocol):
    """Execução: enviar, consultar e cancelar ordens."""

    async def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    async def cancel_order(self, order_id: str) -> OrderResult:
        ...

    async def get_live_orders(self) -> list[OrderResult]:
        ...
