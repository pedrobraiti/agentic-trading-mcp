# Handoff — de onde parei

> **Propósito:** este arquivo serve para que um chat NOVO saiba com precisão "de onde eu parei",
> de forma relativamente detalhada. É o PRIMEIRO arquivo que a próxima sessão lê.
> Mantenha-o vivo e específico — detalhado o bastante para retomar sem reconstruir o raciocínio.

**Última atualização:** 2026-06-21 (VALIDADO contra a conta real)

## Onde parei
Sistema **validado ponta a ponta contra a conta REAL** `U24235856` e funcionando. O healthcheck (`python -m ibkr_agent.healthcheck`) retornou: auth `authenticated:true, connected:true`; conta Pro com `supportsCashQty:true` e `supportsFractions:true`; **saldo US$8.87**; cotação AAPL 297.23; 0 posições. 19 testes passando, ruff limpo. Tudo commitado e no GitHub.

## Contexto mental
Arquitetura travada e CONFIRMADA na prática (ver `.claude/decisions.md`): CPAPI + cashQty (fracionário liberado na conta), hexagonal, OAuth descartado (só Gateway), paper+trava live. 
**Login do gateway** foi o grande atrito — destravou com: restart limpo do gateway + login em aba anônima + sem sessão concorrente (mobile/web deslogados). A build de 2023 do launcher NÃO era o problema (serverVersion runtime = 10.46.1l Jun/2026). Receita completa na memória global `ibkr-gateway-login`. Detalhe: nesta máquina a PAPER não conecta (ssodh 500), mas a conta REAL conecta — usar a real.
O `.env` está apontando p/ a conta real com `TRADING_MODE=live`, mas `TRADING_ALLOW_LIVE=false` e `TRADING_DRY_RUN=true` → leitura segura, nenhuma ordem real dispara.

## Próximo passo concreto (AMANHÃ, mercado aberto)
O MCP `ibkr` JÁ está registrado no Claude Code (escopo local, status Connected). **As tools só aparecem numa SESSÃO NOVA** — então amanhã, ao abrir o projeto, eu já terei `session_status/market_status/get_quote/account_summary/positions/buy/sell/cancel_order/open_orders`.
Roteiro: (0) subir o gateway e logar (aba anônima, conta real) — ver memória `ibkr-gateway-login`; (1) numa sessão nova, rodar as tools de LEITURA pelo agente p/ confirmar; (2) com mercado ABERTO, testar uma ordem real fracionária mínima (cashQty US$1-2) — isso vai disparar warnings de reply; capturar os `messageIds` e adicionar os benignos à allow-list em `broker.py` (hoje só `o354`); lembrar que `.env` está com dry_run=true e allow_live=false → p/ ordem real de teste, ajustar conscientemente. (3) A skill `/invest` (prompt de decisão) é tarefa do usuário. Depois: tickle em background + alerta de reautenticação.

## Em aberto / armadilhas
- Sessão da live expira (~ horas) e cai na manutenção ~01:00 ET; precisa relogar (sem OAuth p/ varejo). Plano: monitor de reauth.
- Gateway precisa estar rodando + logado p/ qualquer coisa funcionar. Gateway extraído em `C:\Users\ACS Gamer\Documents\vscode-local\ibkr-gateway` (fora do repo); subir com `./bin/run.bat root/conf.yaml`.
- allow-list de reply só tem `o354`; mapear outros no teste com mercado aberto.
- bid/ask vieram None (sem subscrição de market data); last_price funciona. Ordens são MKT, então ok.
- Precisão float: saldo já arredondado p/ centavos; positions (mktPrice/avgCost) ainda não — polir quando houver posição real.
- Repo PÚBLICO: `.env` (com a conta real) é gitignored; nunca commitar.

## Como retomar rápido
- Healthcheck: `.venv/Scripts/python.exe -m ibkr_agent.healthcheck` (precisa gateway logado).
- Testes: `.venv/Scripts/python.exe -m pytest -q` | lint: `ruff check .`
- Rodar MCP: `.venv/Scripts/python.exe -m ibkr_agent.server.app`.
- Subir gateway: em `C:\Users\ACS Gamer\Documents\vscode-local\ibkr-gateway` → `./bin/run.bat root/conf.yaml`; login em https://localhost:5000 (aba anônima).
- Memória global: `agentic-trading-architecture`, `ibkr-gateway-login`, `research-channel`.
