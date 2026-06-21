"""Autenticação via Client Portal Gateway (único caminho liberado p/ varejo).

Fluxo: o usuário loga manualmente no navegador (`https://localhost:5000`, com 2FA).
Daqui cuidamos de verificar o status, reinicializar a brokerage session quando
``connected`` mas não ``authenticated``, e manter o keep-alive via ``/tickle``.
"""

from __future__ import annotations

from ..cpapi.client import CpapiClient, CpapiError


class GatewayAuth:
    """Implementa ``AuthPort`` sobre o Client Portal Gateway."""

    def __init__(self, client: CpapiClient):
        self._client = client

    async def status(self) -> dict:
        data = await self._client.post("/iserver/auth/status")
        return data if isinstance(data, dict) else {}

    async def is_authenticated(self) -> bool:
        return bool((await self.status()).get("authenticated"))

    async def ensure_session(self) -> None:
        status = await self.status()
        if status.get("authenticated"):
            await self._confirm_accounts()
            return

        if status.get("connected"):
            # Conectado ao gateway mas sem brokerage session ativa: (re)inicializa.
            await self._client.post(
                "/iserver/auth/ssodh/init", json={"publish": True, "compete": True}
            )
            await self.tickle()
            if await self.is_authenticated():
                await self._confirm_accounts()
                return

        raise CpapiError(
            "Sessão não autenticada. Faça login no Client Portal Gateway em "
            "https://localhost:5000 (com 2FA) e tente de novo."
        )

    async def tickle(self) -> dict:
        """Keep-alive da sessão. Deve ser chamado a cada ~60s. Retorna o payload do /tickle."""
        data = await self._client.post("/tickle")
        return data if isinstance(data, dict) else {}

    async def _confirm_accounts(self) -> None:
        """`GET /iserver/accounts` é pré-requisito antes de qualquer operação de ordem."""
        await self._client.get("/iserver/accounts")
