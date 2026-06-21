"""Cliente HTTP de baixo nível para a CPAPI.

A CPAPI roda atrás de um gateway local com certificado autoassinado, por isso
``verify=False``. O ``base_url`` já inclui ``/v1/api``; para o httpx concatenar
corretamente, normalizamos o base_url com barra final e removemos a barra inicial
do endpoint (senão o httpx descartaria o ``/v1/api``).
"""

from __future__ import annotations

from typing import Any

import httpx


class CpapiError(Exception):
    """Erro de comunicação ou de negócio vindo da CPAPI."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class CpapiClient:
    def __init__(self, base_url: str, *, timeout: float = 15.0):
        normalized = base_url if base_url.endswith("/") else base_url + "/"
        self._client = httpx.AsyncClient(base_url=normalized, verify=False, timeout=timeout)

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
    ) -> Any:
        path = endpoint.lstrip("/")
        try:
            response = await self._client.request(method, path, params=params, json=json)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            payload = _safe_json(exc.response)
            raise CpapiError(
                f"CPAPI {method} {path} falhou: HTTP {exc.response.status_code}",
                status=exc.response.status_code,
                payload=payload,
            ) from exc
        except httpx.HTTPError as exc:
            raise CpapiError(
                f"Falha de comunicação com a CPAPI em {path}: {exc}. "
                "O Client Portal Gateway está rodando e logado?"
            ) from exc

        if not response.content:
            return None
        return _safe_json(response)

    async def get(self, endpoint: str, *, params: dict | None = None) -> Any:
        return await self.request("GET", endpoint, params=params)

    async def post(self, endpoint: str, *, json: Any | None = None) -> Any:
        return await self.request("POST", endpoint, json=json)

    async def delete(self, endpoint: str) -> Any:
        return await self.request("DELETE", endpoint)

    async def aclose(self) -> None:
        await self._client.aclose()


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
