# TODO

Plano vivo do projeto. Tarefas e subtarefas, marcadas conforme concluídas.

## Em progresso
- [ ] Validação empírica no PAPER (com o gateway rodando): autenticação, fracionário/cashQty, campos de summary/positions, quais warnings de reply são benignos

## Próximas
- [ ] Loop de `tickle` em background (keep-alive) — hoje a sessão é garantida via ensure_session() por chamada; autônomo de longa duração pede tickle periódico
- [ ] Tratar feriados no `is_market_open_now` (calendário de mercado)
- [ ] (Opcional) acompanhar preenchimento de ordem (status Filled) e P&L pós-venda
- [ ] (Futuro) adapter de dados ib_async no MarketDataPort
- [ ] (Descartado) OAuth — IBKR não libera p/ varejo (ver decisions.md)

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
