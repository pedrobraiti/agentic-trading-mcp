# Valet — guide for Claude Code

This file is loaded automatically when this repo is opened in Claude Code. If a user
asks you to help them set up or use Valet, follow this guide. Your role is twofold:
**help them install and connect Valet**, and — once it's running — **place the trades
they ask for**. The trading *decision* (what/when) is always the user's; Valet is just
the reliable execution plumbing.

Valet is an MCP server that trades on **Interactive Brokers**, including **fractional
shares by dollar amount**. See `README.md` for the full picture and `DECISIONS.md` for
the reasoning behind the design.

---

## Helping a user set it up

Walk the user through the steps below. Run the commands you can; clearly hand off the
ones only they can do (anything on the IBKR side — you cannot log in for them).

### 1. Install (you can do this)

```bash
python -m venv .venv
# Windows (PowerShell): & ".venv\Scripts\Activate.ps1"
# Linux/macOS:          source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 2. Configure `.env` (you can do this — ask the user for their values)

Set at least `IBKR_ACCOUNT_ID`. Keep the safe defaults: `IBKR_TRADING_MODE=paper`,
`TRADING_ALLOW_LIVE=false`, `TRADING_DRY_RUN=true`. Never commit `.env`.

### 3. The IBKR side (ONLY the user can do this — guide them clearly)

- An **IBKR Pro** account, open and funded (required by the API, even for paper).
- **Fractional permission**: Client Portal → Settings → Trading → Trading Permissions →
  Stocks → check **"Global (Trade in Fractions)"**.
- A **dedicated username** for the bot (IBKR allows one brokerage session per username;
  logging into TWS/mobile with the same user kills the gateway session).
- **Download and start the Client Portal Gateway** (Java app), then **log in via the
  browser** at `https://localhost:5000` with 2FA. This manual login is unavoidable —
  IBKR has no OAuth for retail. See the "Gateway setup" and "Login troubleshooting"
  sections of the README.

### 4. Register the MCP server with Claude Code

```bash
claude mcp add ibkr -- /path/to/.venv/Scripts/python.exe -m ibkr_agent.server.app
```

The tools appear in a **new** Claude Code session.

### 5. Verify

```bash
python -m ibkr_agent.healthcheck   # or: ibkr-healthcheck
```

A healthy result shows `authenticated=True connected=True`, the account flags
(`supportsCashQty`/`supportsFractions`), the balance, and a quote.

---

## Using Valet day to day

- Tools: `session_status`, `market_status`, `get_quote`, `account_summary`,
  `positions`, `buy`, `sell`, `close_position`, `cancel_order`, `open_orders`.
- `buy` takes `cash_amount` (USD, fractional) or `quantity`. `sell` takes `quantity`
  (IBKR doesn't allow selling by dollar amount). `close_position(symbol)` exits 100%.
- Keep the session warm with `python -m ibkr_agent.keepalive` (`ibkr-keepalive`). It
  alerts (`[ALERT] Reauthentication required: ...`) when the user must log in again.

## Safety — read before placing any order

Valet ships safe by default and you must keep it that way:

- **Never** set `TRADING_ALLOW_LIVE=true` or `TRADING_DRY_RUN=false` on your own. Only
  do it if the user explicitly asks, understands it means **real money**, and confirms.
- Orders are blocked outside regular trading hours, above `MAX_ORDER_VALUE`, and when an
  unknown confirmation warning appears.
- Before a real order, confirm the symbol, side, and amount back to the user.

---

## Contributing to Valet itself

If the task is changing Valet's code (not just using it): keep the hexagonal structure
(domain ports, CPAPI adapters, safety guards, MCP server), add tests for new logic
(the suite runs offline), and make sure `ruff check .` and `pytest -q` pass — CI runs
both. Commits follow Conventional Commits. See `CONTRIBUTING.md`.
