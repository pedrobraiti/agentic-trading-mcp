# Valet — guide for Claude Code

This file is loaded automatically when this repo is opened in Claude Code. If a user
asks you to help them set up or use Valet, follow this guide. Your role is twofold:
**help them install and connect Valet**, and — once it's running — **place the trades
they ask for**. The trading *decision* (what/when) is always the user's; Valet is just
the reliable execution plumbing.

Valet is a **monorepo of two MCP servers** over one shared safety core (`trading_core`):
`ibkr` — trades on **Interactive Brokers** (incl. **fractional shares by dollar amount**) —
and `crypto` — spot on **crypto exchanges** via CCXT. See `README.md` for the full picture
and `DECISIONS.md` for the reasoning behind the design. (The setup section below covers the
IBKR server; the crypto server has its own section further down.)

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

## Known issues

### Login goes through but nothing happens

**Symptom:** the user opens `https://localhost:5000`, logs in, approves 2FA, the page
loads — but then nothing happens. It just sits there and never reaches a logged-in
state, and the healthcheck keeps reporting `authenticated=False` / `connected=False`
(sometimes with `ssodh/init` returning HTTP 500 or a `no bridge` error).

**What fixes it:** restart the gateway cleanly and log in fresh. Stop the gateway's Java
process, start it again (`bin\run.bat root\conf.yaml` on Windows, `bin/run.sh
root/conf.yaml` on Linux/macOS), then reload `https://localhost:5000` and log in again.
A clean restart clears this in the large majority of cases — guide the user through it
first. Logging in from an incognito/private browser tab also helps (stale cookies can
get in the way).

**If it still persists:** it can also help to log out of any other IBKR session — IBKR
allows only one brokerage session per username, so a session open in IBKR Mobile or the
Client Portal web can block the gateway. Have the user log those out, restart the
gateway once more, and try again.

**Important — the login is not sticky.** Every time a fresh login is needed (the session
expired, the machine slept, the daily maintenance window passed), do the clean restart
*first*; don't just retry the login against the gateway that's already running. The
sequence is always: restart the gateway → then log in.

---

## Using Valet day to day

These are **all 20 MCP tools** you have once the `ibkr` server is connected — your full
capability surface. Every tool returns an `{"ok": bool, "data"/"error": ...}` envelope.

**Session & market**
- `session_status` — is the gateway authenticated/connected/competing, **and which
  account is live**: it returns `account_type` (`"LIVE"`/`"PAPER"`) from IBKR's
  `isPaper` (a LIVE account also returns a `warning`). This is the ground truth — the
  `IBKR_TRADING_MODE` label can disagree. Check it before trading so you never mistake
  a real-money account for paper. `portfolio` carries the same `account_type`.
- `market_status` — is the US market open (RTH) right now.

**Quotes & account (read-only)**
- `get_quote(symbol)` — last/bid/ask for one symbol.
- `get_quotes(symbols)` — quote a whole watchlist in **one** call (cheaper than N `get_quote`).
- `account_summary` — available funds, net liquidation, buying power.
- `positions` — open positions.
- `portfolio` — account summary + positions + total unrealized P&L in one snapshot.

**Before committing**
- `preview_order(symbol, side, cash_amount|quantity, limit_price?)` — IBKR `whatif`:
  estimated commission, cost, available-funds impact and warnings **without sending**.

**Placing orders**
- `buy(symbol, cash_amount|quantity, limit_price?)` — market by default; `cash_amount`
  is fractional via cashQty; pass `limit_price` for a LIMIT (needs `quantity`).
- `sell(symbol, quantity, limit_price?)` — by shares (IBKR forbids selling by dollar amount).
- `close_position(symbol)` — exits 100% of a position at the exact fractional quantity.
- `stop_order(symbol, side, quantity, stop_price, limit_price?)` — a STOP (stop-loss),
  or STOP-LIMIT if `limit_price` is given.
- `trailing_stop(symbol, side, quantity, trail_amount|trail_percent)` — a stop that
  follows the price (locks in gains as it moves).
- `bracket_order(symbol, quantity, take_profit, stop_loss, side?, entry_limit_price?)` —
  an entry with attached take-profit + stop-loss exits (OCO: one fills, the other cancels).

**After placing**
- `order_status(order_id)` — state, filled quantity, average price (use it to confirm a
  fill; `positions` lags right after a trade).
- `wait_for_fill(order_id, timeout_seconds?)` — poll until it fills (or is cancelled/
  rejected), so you don't orchestrate the retry yourself (timeout capped at 120s).
- `open_orders` — active orders. `cancel_order(order_id)` — cancel one.
- `trade_history(limit?)` — local audit log of every attempt (sent, dry-run, blocked).
- `reconcile_pending(resolve_missing?)` — reconcile **dispatched-but-unconfirmed** orders against
  IBKR's open orders. After a timeout/crash the safety layer blocks an identical resend until the
  pending intent is reconciled; this clears it (resting orders → resolved; not-found → stay blocked,
  since resending blind could double a fill).

**Order types supported:** market, limit, stop, stop-limit, trailing-stop, and brackets —
plus fractional **buys by dollar amount** (cashQty) and fractional sells by quantity.

**Session upkeep:** the MCP server keeps its own session warm (background `/tickle`). For
headless/scheduled use there's also `python -m ibkr_agent.keepalive` (`ibkr-keepalive`),
which alerts (`[ALERT] Reauthentication required: ...`) when the user must log in again.

## The crypto server (CCXT) — a second, independent MCP

This repo is a **monorepo of two execution servers** over one shared core (`trading_core`):
`ibkr` (above) and **`crypto`** (spot, via CCXT). They are **separate processes** with their
own tools and login — they only share code. Crypto exists because it fixes IBKR's structural
pain: a **persistent API key** (no gateway, no browser login, no tickle), a **24/7** market,
and `ccxt` behind one interface.

### Setup (crypto)

1. Fill the `CRYPTO_*` keys in `.env` (mirrored in `.env.example`). Default
   `CRYPTO_EXCHANGE=binance`, `CRYPTO_TRADING_MODE=sandbox`, `CRYPTO_QUOTE_CCY=USDT`,
   `CRYPTO_ALLOW_MARGIN=false`, `CRYPTO_ALLOW_LIVE=false`.
2. **Sandbox = the exchange testnet** (Binance has one): free, separate API keys, fake
   money — **no deposit needed**. This is the paper-first mode; validate here first.
3. Register the server (separate from `ibkr` — tools are prefixed per server, so they never
   collide):
   ```bash
   claude mcp add crypto -- /path/to/.venv/Scripts/python.exe -m crypto_agent.server.app
   ```
4. Verify: `python -m crypto_agent.healthcheck` (or `crypto-healthcheck`) — reports exchange,
   mode (sandbox/live), the quote-currency balance and a sample quote.

### Crypto tools (mirror the IBKR names)

`session_status` (probes the keys; returns `account_type` LIVE/PAPER and, when live, a
`warning`), `market_status` (always open), `get_quote`/`get_quotes`, `account_summary`,
`positions`, `portfolio`, `buy` (`cash_amount` in the quote ccy via
`createMarketBuyOrderWithCost`, **or** `quantity` in the base; market or LIMIT),
`sell` (by `quantity`), `stop_order` (**exchange-native** trigger order via CCXT's unified
`triggerPrice` — rests on the exchange and fires with no agent running; most spot APIs only
offer stop-LIMIT, so pass `limit_price`; refused cleanly where the exchange has no native
stops), `close_position` (sells 100% of the base balance),
`order_status`, `cancel_order`, `open_orders`, `trade_history`, `reconcile_pending` (clears the
resend-block on a dispatched-but-unconfirmed order, same as IBKR). **Not** offered on crypto:
brackets, trailing stops, `preview_order` (no exchange whatif).

### Crypto safety specifics

- **Spot-only** by default (`CRYPTO_ALLOW_MARGIN=false`): no margin/leverage, and selling
  more than you hold (a short) is blocked.
- **Separate live gate:** real-money crypto needs **both** `CRYPTO_TRADING_MODE=live` **and**
  `CRYPTO_ALLOW_LIVE=true` — independent of the IBKR `TRADING_ALLOW_LIVE`. Arming IBKR does
  not arm crypto.
- **Separate dry-run:** `CRYPTO_DRY_RUN` (default `true`) is independent of the IBKR
  `TRADING_DRY_RUN`, so crypto stays safe-by-default even when IBKR's dry-run is off.
- The shared **policy limits** (`MAX_ORDER_VALUE`, `MAX_DAILY_VALUE`,
  `DUPLICATE_WINDOW_SECONDS`) apply to crypto too; `MAX_ORDER_VALUE` is read in the **quote
  currency** (e.g. USDT). The live/dry-run *arms* are per-venue.
- **Real-money caveat:** unlike IBKR's native paper account, not every exchange has a
  sandbox. With `CRYPTO_TRADING_MODE=live`, even with dry-run on, the agent is one step from
  real money. Also note exchanges enforce a **minimum notional per pair** (Binance spot
  ~5 USDT) — a sub-minimum order is rejected by the exchange (the adapter catches it first).

## Safety — read before placing any order

Valet ships safe by default and you must keep it that way:

- **Never** set `TRADING_ALLOW_LIVE=true` or `TRADING_DRY_RUN=false` on your own. Only
  do it if the user explicitly asks, understands it means **real money**, and confirms.
- Orders are blocked outside regular trading hours, above `MAX_ORDER_VALUE`, and when an
  unknown confirmation warning appears.
- The guard also **fails closed** on account/identity problems: configured account ≠
  logged-in account, a real (`isPaper=false`) account not armed with
  `TRADING_ALLOW_LIVE=true`, or `IBKR_TRADING_MODE` disagreeing with the real account. It
  also blocks an accidental short (SELL > held position, unless `TRADING_ALLOW_SHORT=true`)
  and an inverted stop (one that would trigger immediately).
- Before a real order, confirm the symbol, side, and amount back to the user.
- **Never assume paper vs. live from the config or defaults.** Call `session_status`
  and read `account_type` — if it's `LIVE`, you are moving real money; say so plainly
  to the user. Paper account ids start with `DU`, live ones with `U`.

---

## Contributing to Valet itself

If the task is changing Valet's code (not just using it): keep the hexagonal structure
(domain ports, CPAPI adapters, safety guards, MCP server), add tests for new logic
(the suite runs offline), and make sure `ruff check .` and `pytest -q` pass — CI runs
both. Commits follow Conventional Commits. See `CONTRIBUTING.md`.
