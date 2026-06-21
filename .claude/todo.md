# TODO

Plano vivo do projeto. Tarefas e subtarefas, marcadas conforme concluídas.

## Em progresso
- [ ] (AMANHÃ, mercado aberto) Numa sessão NOVA do Claude Code: usar as tools do MCP `ibkr` (já registrado) — session_status, market_status, get_quote, account_summary, positions — e depois testar uma ordem real fracionária mínima

## Próximas
- [ ] Testar uma ordem real fracionária ($1-2 em cashQty) com mercado ABERTO — mapear quais warnings de reply (messageIds) aparecem e ajustar a allow-list além do `o354`
- [ ] Loop de `tickle` em background + monitor que avisa quando a sessão cair (reautenticar)
- [ ] Tratar feriados no `is_market_open_now` (calendário de mercado)
- [ ] Polir precisão de positions (mktPrice/avgCost vêm como float) quando houver posições reais
- [ ] (Futuro) acompanhar preenchimento de ordem (status Filled) e P&L pós-venda
- [ ] (Descartado) OAuth — IBKR não libera p/ varejo; auth só Gateway (ver decisions.md)

## Concluído
- [x] Setup inicial do projeto
- [x] Estudo do quick_invest e definição da arquitetura (ver decisions.md)
- [x] Scaffold hexagonal (pyproject, .venv 3.12, estrutura de pastas src/ibkr_agent)
- [x] `domain/` — modelos (OrderRequest quantity|cash_qty, Quote, Position, AccountSummary, OrderResult) + ports (BrokerPort, MarketDataPort, AuthPort) + testes do domínio (5 passando)
- [x] `config.py` (pydantic-settings) + `.env.example` (sem OAuth, doc de gateway/username dedicado)
- [x] `adapters/cpapi/` — client (httpx, base_url normalizado, verify=False), GatewayAuth (status/ssodh-init/tickle/accounts), MarketData (resolve_conid c/ cache, get_quote c/ warmup, summary, positions paginado), Broker (place_order cashQty|quantity + loop de reply c/ allow-list, cancel, live orders). 11 testes passando (respx), ruff limpo
- [x] `safety/` — GuardedBroker (decorator do BrokerPort): live lock, dry-run padrão, limite de valor (notional via quote p/ quantity), RTH; market_hours. Testes com fakes
- [x] `server/` — FastMCP (mcp 1.28) com tools (session_status, market_status, get_quote, account_summary, positions, buy, sell, cancel_order, open_orders) + composition root (build_services); console script `mcp-ibkr-agent`. Smoke tests. 19 testes no total
- [x] README completo (setup gateway, username dedicado, fracionário, registro no Claude Code) + LICENSE MIT
- [x] VALIDAÇÃO REAL: sistema testado contra a conta live U24235856 — auth/connected OK, supportsCashQty/supportsFractions=True (Pro), saldo US$8.87, cotação e posições funcionando. Build de 2023 não é problema (serverVersion runtime = 10.46.1l Jun/2026)
- [x] `healthcheck` (módulo + console script `ibkr-healthcheck`): relatório de conexão/conta/saldo. Fix de precisão de saldo (arredonda p/ centavos) e de encoding (sem emoji, console Windows cp1252)
- [x] `config.py` acha o `.env` por caminho ABSOLUTO (funciona quando o Claude Code lança o MCP de outro CWD)
- [x] MCP `ibkr` REGISTRADO no Claude Code (escopo local, `claude mcp add`) — status Connected. Tools aparecem numa sessão NOVA
