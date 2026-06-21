# Handoff — de onde parei

> **Propósito:** este arquivo serve para que um chat NOVO saiba com precisão "de onde eu parei",
> de forma relativamente detalhada. É o PRIMEIRO arquivo que a próxima sessão lê.
> Mantenha-o vivo e específico — detalhado o bastante para retomar sem reconstruir o raciocínio.

**Última atualização:** 2026-06-21 (scaffold + domínio prontos)

## Onde parei
Setup feito e repo público no ar (`pedrobraiti/mcp-ibkr-agent`). Scaffold montado:
- `.venv` com Python 3.12.10, `pyproject.toml` (deps: mcp, httpx, pydantic, pydantic-settings, python-dotenv; dev: pytest, pytest-asyncio, ruff, respx).
- Estrutura hexagonal em `src/ibkr_agent/`: `domain/`, `adapters/`, `safety/`, `server/`.
- **Domínio pronto:** `domain/models.py` (OrderRequest com regra "exatamente um entre quantity e cash_qty", Quote, Position, AccountSummary, OrderResult, enums) e `domain/ports.py` (BrokerPort, MarketDataPort, AuthPort como Protocols async).
- `tests/test_order_request.py` — 5 testes passando.
Tudo commitado.

## Contexto mental
Arquitetura travada (ver `.claude/decisions.md`): CPAPI + cashQty, hexagonal, OAuth-alvo/Gateway-fallback, paper+trava live. O domínio é puro e não depende da API. O próximo bloco (adapter CPAPI) depende de detalhes concretos da API da IBKR que eu NÃO quero chutar — por isso mandei ao usuário um prompt de pesquisa (canal de pesquisa profunda, ver CLAUDE.md) cobrindo: payload exato do POST de ordem com cashQty, fluxo de reply de confirmação de ordem da CPAPI, mecânica de sessão/keep-alive `/tickle` e auth do Gateway, e o "aquecimento" do snapshot de market data.

## Próximo passo concreto
Quando o relatório de pesquisa da CPAPI chegar: implementar `adapters/cpapi/` (client httpx async) satisfazendo os três ports, começando por AuthPort (Gateway + /tickle) e MarketDataPort (resolve_conid, get_quote, get_account_summary, get_positions), depois BrokerPort (place_order com quantity|cashQty + tratamento do reply de confirmação, cancel_order, get_live_orders). Enquanto o relatório não chega, dá p/ adiantar a camada `safety/` (paper/live, dry-run, limite MAX_ORDER_VALUE) e o esqueleto do `server/` MCP com um adapter fake p/ testar as tools.

## Em aberto / armadilhas
- Detalhes da CPAPI a confirmar via pesquisa (acima) — não implementar o adapter no chute.
- OAuth retail pode exigir liberação da IBKR → manter Gateway como fallback.
- Validar no paper se "Trade in Fractions" espelha da live e se cashQty funciona em paper.
- Snapshot de market data da CPAPI costuma vir vazio na 1ª chamada (aquecer/repetir).
- Repo PÚBLICO: nunca commitar segredo; só `.env.example` sem valores. Credenciais reais ficam no `.env` local (gitignored), reaproveitadas do quick_invest.

## Como retomar rápido
- Ler `.claude/decisions.md` (porquês) e `.claude/todo.md` (plano).
- Rodar testes: `.venv/Scripts/python.exe -m pytest -q`.
- Referência de código CPAPI (a reescrever limpo): `G:\Meu Drive\vscode\quick_invest\services\ib_service.py`.
- Ativar venv (Windows/PowerShell): `& ".venv\Scripts\Activate.ps1"` (se erro de policy: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`).
