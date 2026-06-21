# Handoff — de onde parei

> **Propósito:** este arquivo serve para que um chat NOVO saiba com precisão "de onde eu parei",
> de forma relativamente detalhada. É o PRIMEIRO arquivo que a próxima sessão lê.
> Mantenha-o vivo e específico — detalhado o bastante para retomar sem reconstruir o raciocínio.

**Última atualização:** 2026-06-21 (MVP do servidor MCP completo)

## Onde parei
MVP funcional end-to-end commitado e no GitHub (`pedrobraiti/mcp-ibkr-agent`). 19 testes passando, ruff limpo. Pronto:
- **domain/** — modelos + ports.
- **adapters/cpapi/** — client, GatewayAuth, CpapiMarketData, CpapiBroker (com loop de reply + allow-list).
- **safety/** — GuardedBroker (live lock, dry-run, limite de valor, RTH) + market_hours.
- **server/** — FastMCP (mcp 1.28) com 9 tools + composition root (`build_services`). Console script `mcp-ibkr-agent`.
- README completo + LICENSE MIT.

O código está **completo mas NÃO validado contra a IBKR real** — todos os testes usam mocks (respx) e fakes. Falta a validação empírica no paper (precisa do gateway rodando + login).

## Contexto mental
Arquitetura travada (ver `.claude/decisions.md`): CPAPI + cashQty, hexagonal, **OAuth descartado** (auth só Gateway+tickle), paper+trava live. Decisões do adapter vieram do relatório de pesquisa da CPAPI (loop de reply, warmup duplo, /iserver/accounts antes de operar, username dedicado, rate limit 10 req/s, manutenção ~01:00). Auth hoje é garantida via `ensure_session()` no início de cada tool — sem loop de tickle em background ainda (suficiente p/ /invest manual; autônomo de longa duração vai querer o tickle periódico).

## Próximo passo concreto
Validação no PAPER, com o usuário: subir o Client Portal Gateway, logar, preencher `.env` (IBKR_ACCOUNT_ID real do paper), registrar o MCP no Claude Code e rodar tool a tool (`session_status` → `market_status` → `get_quote AAPL` → `account_summary` → `buy AAPL cash_amount=... ` em dry-run, depois desligar dry-run). Conferir e fixar: nomes de campo de `/portfolio/.../summary` e `/positions`; se cashQty funciona em paper; quais `messageIds` de warning aparecem (atualizar allow-list em broker.py além do `o354`). Só depois, considerar o loop de tickle em background.

## Em aberto / armadilhas
- Tudo testado só com mock — comportamento real da CPAPI pode diferir em nomes de campo/fluxo. Validar no paper antes de confiar.
- allow-list de reply hoje = só `o354`; warning desconhecido bloqueia (proposital). Mapear outros benignos no paper.
- `is_market_open_now` ainda não trata feriados.
- Conta live precisa estar aberta/fundeada/IBKR Pro até p/ usar paper.
- Repo PÚBLICO: segredos só no `.env` local (gitignored).

## Como retomar rápido
- Testes: `.venv/Scripts/python.exe -m pytest -q` | lint: `.venv/Scripts/python.exe -m ruff check .`
- Rodar o MCP: `.venv/Scripts/python.exe -m ibkr_agent.server.app` (precisa do gateway logado).
- Registrar no Claude Code: `claude mcp add ibkr -- <abs>/.venv/Scripts/python.exe -m ibkr_agent.server.app`
- Estrutura: `src/ibkr_agent/{domain,adapters/cpapi,safety,server}/`.
- Relatórios de pesquisa (fracionário + CPAPI) estão no histórico da conversa; novo prompt de pesquisa → entregar ao usuário (ver CLAUDE.md).
