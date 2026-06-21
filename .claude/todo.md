# TODO

Plano vivo do projeto. Tarefas e subtarefas, marcadas conforme concluídas.

## Em progresso
- [ ] `adapters/cpapi/` — cliente CPAPI (auth Gateway, keep-alive /tickle, get_quote, get_balance, get_positions, place_order com cashQty/quantity, cancel_order) — AGUARDANDO relatório de pesquisa sobre payload exato e fluxo de auth

## Próximas
- [ ] Camada de segurança — paper/live flag, dry-run padrão, confirmação e limite de valor
- [ ] Servidor MCP — expor as tools sobre os ports
- [ ] Testes unitários (lógica de domínio/segurança) + integração (adapter CPAPI mockado)
- [ ] `.env.example` espelhado, README com instruções de setup e de habilitar fracionário
- [ ] (Futuro) adapter OAuth no AuthPort; (futuro) adapter de dados ib_async

## Concluído
- [x] Setup inicial do projeto
- [x] Estudo do quick_invest e definição da arquitetura (ver decisions.md)
- [x] Scaffold hexagonal (pyproject, .venv 3.12, estrutura de pastas src/ibkr_agent)
- [x] `domain/` — modelos (OrderRequest quantity|cash_qty, Quote, Position, AccountSummary, OrderResult) + ports (BrokerPort, MarketDataPort, AuthPort) + testes do domínio (5 passando)
