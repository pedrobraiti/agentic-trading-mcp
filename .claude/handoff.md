# Handoff — de onde parei

> **Propósito:** este arquivo serve para que um chat NOVO saiba com precisão "de onde eu parei",
> de forma relativamente detalhada. É o PRIMEIRO arquivo que a próxima sessão lê.
> Mantenha-o vivo e específico — detalhado o bastante para retomar sem reconstruir o raciocínio.

**Última atualização:** 2026-06-22 (ROUND-TRIP REAL executado com mercado aberto)

## ▶️ AO RECEBER "continue" (runbook de inicialização — FAZER ISTO PRIMEIRO)
O usuário vai só mandar "continue". Eu (agente) executo automaticamente, nesta ordem:
1. **Conferir se o gateway já está no ar:** `curl -sk --max-time 3 https://localhost:5000/v1/api/one/user -o /dev/null -w "%{http_code}"`. Se voltar `000`, **subir o gateway eu mesmo** (background):
   `cd "C:/Users/ACS Gamer/Documents/vscode-local/ibkr-gateway" && ./bin/run.bat root/conf.yaml` (run_in_background) e esperar a porta responder (HTTP 401 = no ar, esperando login).
2. **Conferir autenticação:** `.venv/Scripts/python.exe -m ibkr_agent.healthcheck`. Se disser "Sessao nao autenticada":
   - **PEDIR AO USUÁRIO para logar** (única coisa que EU NÃO posso fazer — é navegador + 2FA): abrir `https://localhost:5000` em **aba anônima**, conta **real**, **IBKR Mobile deslogado** (sessão concorrente trava). Esperar ele dizer "logado".
   - Se travar no 2FA, ver receita em [[ibkr-gateway-login]] (restart limpo do gateway + aba anônima + sem sessão concorrente; Challenge/Response se push falhar).
3. **Depois do login:** rodar o healthcheck de novo p/ confirmar `authenticated:true`, e então seguir o "Próximo passo concreto" abaixo.
4. As tools do MCP `ibkr` já estão disponíveis nesta sessão nova (foi registrado ontem) — usar direto (session_status, market_status, get_quote, account_summary, positions, buy, sell...).

OBS: o gateway pode ter caído (PC desligou / manutenção ~01:00 ET / sessão de 24h expirou) — por isso quase sempre vou precisar subir o gateway E pedir o login de novo. Isso é o normal da CPAPI de varejo.

## Onde parei
**Round-trip REAL executado com mercado aberto** na conta `U24235856`. Comprei US$2 de AAPL via `cashQty` (ordem 864501253 Filled, 0.0066 @ 298.96) e depois fechei a posição (ordem 864501623 Filled, vendi 0.0066 @ 300.41). Caixa de volta a **US$8.84, flat** (`stockmarketvalue:0.0` no ledger — confirmado; o endpoint de positions tem cache lento e mostrou a posição "fantasma" por um tempo). 19 testes passando, ruff limpo.
**Allow-list de reply mapeada AO VIVO** e já commitada em `broker.py`: `o354, o10164, o10223, o10151, o10153` (warnings padrão de MKT+cashQty).
**DESCOBERTA grande:** `cashQty` é **só para COMPRA** — a IBKR rejeita cashQty em venda (`"Cash order quantity can not be set for sell order"`). Venda fracionária tem que ser por **quantidade de ações fracionária**. Ver `decisions.md` 2026-06-22.

## Contexto mental
Arquitetura confirmada na prática (ver `.claude/decisions.md`): CPAPI + cashQty (compra fracionária), hexagonal, OAuth descartado (só Gateway), trava live.
**Login do gateway** destrava com: restart limpo + aba anônima + sem sessão concorrente (mobile/web deslogados). Build de 2023 do launcher NÃO é problema (serverVersion runtime = 10.46.1l Jun/2026). Receita na memória global [[ibkr-gateway-login]]. Nesta máquina a PAPER não conecta (ssodh 500); a REAL conecta — usar a real.
O `.env` fica com `TRADING_MODE=live`, `TRADING_ALLOW_LIVE=false`, `TRADING_DRY_RUN=true` → leitura segura. Para o teste real eu **NÃO** mexi no `.env`: passei `TRADING_ALLOW_LIVE=true TRADING_DRY_RUN=false` por variável de ambiente num script temporário (já deletado), preservando a trava.

## Próximo passo concreto
**Implementar venda fracionária** (é o gap que o teste revelou — hoje o agente NÃO consegue fechar posição fracionária):
1. `OrderRequest.quantity`: `int` → `Decimal` (aceitar ações fracionárias). Manter `gt=0`. Atualizar validador/docstring.
2. `broker._build_order`: `order["quantity"] = float(request.quantity)`.
3. Tools MCP `buy`/`sell` em `server/app.py`: param `quantity` de `int` → `float`; no `sell`, como cashQty não vale, converter `cash_amount`→ações via cotação OU exigir quantidade. Idealmente nova tool `close_position(symbol)` que lê o tamanho exato e vende tudo (evita o warning `o2137` de oversell).
4. Ajustar guard (notional via quote para quantity) e testes (`test_order_request`, `test_cpapi_broker`).
Depois disso: enviar `Decline` (confirmed:false) ao bloquear warning (não deixar ordem `Inactive` órfã); tickle em background + alerta de reauth. A skill `/invest` (decisão) é tarefa do usuário.

## Em aberto / armadilhas
- **`o2137`** (venda > posição) propositalmente FORA da allow-list global — auto-confirmar oversell é perigoso. Fechar posição = vender quantidade exata, sem o warning.
- Ordens bloqueadas deixam órfãs `Inactive` na conta (não executam, não dá pra cancelar — HTTP 400; somem sozinhas). Resolver com Decline ao bloquear.
- Endpoint de `positions` tem **cache lento** — após operar, confirmar flat pelo **ledger** (`/portfolio/{acct}/ledger` → `stockmarketvalue`/`cashbalance`), não só por positions.
- Sessão da live expira e cai na manutenção ~01:00 ET; precisa relogar (sem OAuth p/ varejo).
- Gateway extraído em `C:\Users\ACS Gamer\Documents\vscode-local\ibkr-gateway` (fora do repo); subir com `./bin/run.bat root/conf.yaml`.
- bid/ask às vezes None (sem subscrição de market data); last_price funciona. Ordens são MKT, então ok.
- Precisão float: saldo arredondado p/ centavos; positions (mktPrice/avgCost) ainda não — polir depois.
- Repo PÚBLICO: `.env` (conta real) é gitignored; nunca commitar.

## Como retomar rápido
- Healthcheck: `.venv/Scripts/python.exe -m ibkr_agent.healthcheck` (precisa gateway logado).
- Testes: `.venv/Scripts/python.exe -m pytest -q` | lint: `ruff check .`
- Rodar MCP: `.venv/Scripts/python.exe -m ibkr_agent.server.app`.
- Subir gateway: em `C:\Users\ACS Gamer\Documents\vscode-local\ibkr-gateway` → `./bin/run.bat root/conf.yaml`; login em https://localhost:5000 (aba anônima).
- Memória global: `agentic-trading-architecture`, `ibkr-gateway-login`, `research-channel`.
