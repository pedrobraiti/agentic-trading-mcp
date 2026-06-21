# mcp-ibkr-agent

Servidor **MCP** que dá a um agente de IA (como o Claude Code) a capacidade de operar na **Interactive Brokers**: consultar cotações, saldo, posições e ordens, e **comprar/vender** — inclusive **ações fracionárias por valor em dólar** (`cashQty`) via Client Portal API.

A *decisão* de investimento (o que/quando comprar ou vender) fica com você e com o prompt da sua skill. Este projeto entrega só o **encanamento de trading confiável** — com travas de segurança por padrão.

> ⚠️ **Não é aconselhamento financeiro.** Opera em conta *paper* por padrão; operação *live* exige destravar explicitamente. Use por sua conta e risco.

## Arquitetura

Hexagonal (ports & adapters):

```
domain/      modelos (OrderRequest com quantity OU cash_qty) e portas (Broker/MarketData/Auth)
adapters/    cpapi/ — implementação sobre a IBKR Client Portal API (REST)
safety/      GuardedBroker — travas: live lock, dry-run, limite de valor, horário de pregão
server/      servidor MCP (FastMCP) + composição das dependências
```

Trocar/estender o broker no futuro (ex.: um adapter de dados em `ib_async`) é mexer só em `adapters/` + `server/services.py`.

## Pré-requisitos

- **Python 3.12+**
- Conta **Interactive Brokers** aberta, fundeada e do tipo **IBKR Pro** (exigência da API, mesmo para usar o paper associado).
- **Permissão de fracionário** habilitada: Client Portal → Settings → Trading → Trading Permissions → seção Stocks → marcar **"Global (Trade in Fractions)"**.
- **IBKR Client Portal Gateway** rodando localmente (Java 8u192+).
- **Username dedicado ao bot**: a IBKR permite só **uma** brokerage session por username — logar no TWS/celular com o mesmo usuário derruba a sessão do gateway.

## Instalação

```bash
git clone https://github.com/pedrobraiti/mcp-ibkr-agent.git
cd mcp-ibkr-agent
python -m venv .venv
# Windows (PowerShell): & ".venv\Scripts\Activate.ps1"   (se erro de policy: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)
# Linux/macOS:          source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # preencha IBKR_ACCOUNT_ID etc.
```

## Configuração (`.env`)

Veja `.env.example`. Principais chaves:

| Chave | Default | Descrição |
|---|---|---|
| `IBKR_API_BASE_URL` | `https://localhost:5000/v1/api` | Endpoint do Client Portal Gateway |
| `IBKR_ACCOUNT_ID` | — | ID da conta (ex.: `DU1234567` em paper) |
| `IBKR_TRADING_MODE` | `paper` | `paper` ou `live` |
| `TRADING_ALLOW_LIVE` | `false` | Trava dura: `live` só opera se `true` |
| `TRADING_DRY_RUN` | `true` | Valida mas **não envia** ordens |
| `MAX_ORDER_VALUE` | `100.0` | Limite (US$) por ordem |

## Rodando

1. Inicie o Client Portal Gateway e faça **login no navegador** em `https://localhost:5000` (com 2FA).
2. Registre o servidor MCP no Claude Code:

   ```bash
   claude mcp add ibkr -- /caminho/para/.venv/Scripts/python.exe -m ibkr_agent.server.app
   ```

   (ou rode direto para testar: `python -m ibkr_agent.server.app`)

## Tools expostas

`session_status`, `market_status`, `get_quote`, `account_summary`, `positions`, `buy`, `sell`, `cancel_order`, `open_orders`.

`buy`/`sell` aceitam `cash_amount` (US$, fracionário) **ou** `quantity` (ações inteiras).

## Segurança (padrões)

- **paper** por padrão; **live** bloqueado a menos que `TRADING_ALLOW_LIVE=true`.
- **dry-run** ligado por padrão (não envia ordem de verdade).
- Ordem acima de `MAX_ORDER_VALUE` é recusada.
- Ordens só durante o pregão (RTH).
- Warnings de confirmação da CPAPI só são auto-aceitos via allow-list; warning desconhecido **bloqueia** a ordem.

## Desenvolvimento

```bash
python -m pytest -q          # testes
python -m ruff check .       # lint
```

## Licença

MIT.
