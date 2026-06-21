"""Healthcheck da conexão com a IBKR.

Verifica se o Client Portal Gateway está logado e conectado, e imprime um relatório
legível (versão do servidor, conta, flags de fracionário, saldo e uma cotação).

Uso: python -m ibkr_agent.healthcheck   (com o gateway rodando e logado)
"""

from __future__ import annotations

import asyncio
import warnings

from .adapters.cpapi import CpapiClient, CpapiMarketData, GatewayAuth
from .config import get_settings

warnings.filterwarnings("ignore")


async def _run() -> int:
    settings = get_settings()
    client = CpapiClient(settings.ibkr_api_base_url, timeout=settings.request_timeout_seconds)
    auth = GatewayAuth(client)
    try:
        status = await auth.status()
        server = (status.get("serverInfo") or {}).get("serverVersion", "?")
        print(f"Servidor: {server}")
        print(
            f"Auth: authenticated={status.get('authenticated')} "
            f"connected={status.get('connected')} competing={status.get('competing')}"
        )
        if not status.get("authenticated"):
            print("\n[AVISO] Sessao nao autenticada.")
            print("   Faca login em https://localhost:5000 (com o gateway rodando) e rode de novo.")
            return 1

        accounts = await client.get("/iserver/accounts")
        acct = accounts.get("selectedAccount") if isinstance(accounts, dict) else None
        props = (accounts.get("acctProps", {}) or {}).get(acct, {}) if acct else {}
        print(f"\nConta: {acct}  (paper={accounts.get('isPaper')})")
        print(
            f"  supportsCashQty={props.get('supportsCashQty')} "
            f"supportsFractions={props.get('supportsFractions')} "
            f"lite={props.get('liteUnderPro')}"
        )

        market = CpapiMarketData(client, acct or settings.ibkr_account_id)
        summary = await market.get_account_summary()
        print(
            f"\nSaldo: US${summary.available_funds}  "
            f"(net liq US${summary.net_liquidation}, buying power US${summary.buying_power})"
        )

        quote = await market.get_quote("AAPL")
        if quote:
            print(f"Cotação AAPL: last={quote.last_price} bid={quote.bid} ask={quote.ask}")

        positions = await market.get_positions()
        print(f"Posições abertas: {len(positions)}")

        print("\n[OK] Conexao saudavel.")
        return 0
    finally:
        await client.aclose()


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
