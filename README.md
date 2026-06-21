# mcp-ibkr-agent

Servidor MCP que permite a um agente de IA (Claude Code) operar na Interactive Brokers — consultar cotações, saldo, posições e P&L, e executar ordens de compra/venda, incluindo **ações fracionárias por valor em dólar** (`cashQty`) via Client Portal API.

> ⚠️ Projeto pessoal de automação de investimentos. **Não é aconselhamento financeiro.** Opera em conta *paper* por padrão; operação *live* fica atrás de travas explícitas. Use por sua conta e risco.

## Como funciona
O usuário invoca uma skill (ex.: `/invest`) no Claude Code. O Claude usa as tools deste MCP server para analisar o mercado e executar ordens na IBKR. A lógica de *decisão* (o que comprar/vender, quando, com base em quê) vive na skill/prompt do usuário — este repositório fornece apenas o encanamento de trading confiável.

## Como rodar
_A preencher conforme o projeto evolui (requer Python 3.12+, o IBKR Client Portal Gateway rodando, e variáveis em `.env` — ver `.env.example`)._

## Stack
- Python 3.12+
- Protocolo MCP (servidor de tools)
- Interactive Brokers Client Portal API (CPAPI / Web API REST)
- Arquitetura hexagonal (ports & adapters)

## Status
Em desenvolvimento inicial. Ver `.claude/todo.md` para o plano.
