# Architecture Decision Records

A log of the key technical decisions and the *why* behind them. Append-only:
older entries are not edited, so the reasoning trail stays honest.

---

## ADR-001 — Interface = a dedicated MCP server

**Decision.** Expose trading as native MCP tools (`get_quote`, `account_summary`,
`positions`, `buy`, `sell`, `cancel_order`, ...) that an AI agent calls directly,
instead of a Bash/CLI the agent has to shell out to.

**Why.** Cleaner and safer for an `/invest` skill: the agent reasons over typed
tool results rather than parsing CLI text. This is the "IBKR MCP" the project set
out to build.

**Alternatives considered.** A Python CLI (simpler, but less integrated); a
lib+CLI+MCP hybrid (overengineering for the start).

---

## ADR-002 — Broker connection = Client Portal API (CPAPI), not TWS/ib_async

**Decision.** Connect through the IBKR Client Portal API (REST, local gateway),
not the TWS API / `ib_async`.

**Why.** The hard requirement is **fractional** orders (invest by dollar amount,
DCA, rebalancing). That is only officially supported via CPAPI's `cashQty` field.
The TWS API / `ib_async` has ambiguous and historically broken support for
fractional equity (errors 10242/10243, decimal quantities rejected/truncated).
Key insight: a previous prototype already used CPAPI — it just sent `quantity`
(whole shares) instead of `cashQty`. So this isn't a migration, it's using the
right field.

**Alternatives considered.** `ib_async`/TWS (reverted — worse for fractional);
FIX API (institutional, out of scope).

---

## ADR-003 — Hexagonal architecture (ports & adapters), CPAPI-only for now

**Decision.** Define `BrokerPort`, `MarketDataPort`, `AuthPort` and implement all
of them over CPAPI. Robustness comes from the **design**, not from duplicating
connections.

**Why.** The ports let a future data adapter (e.g. `ib_async`) be plugged in
without rewriting the domain. A CPAPI-execution + `ib_async`-data hybrid *now* was
rejected: with headless auth it would need two headless sessions and two usernames
(the "one brokerage session per username" rule) — a foot-gun and bad for a
reproducible public repo.

---

## ADR-004 — Auth = Gateway login only (OAuth discarded for retail)

**Decision.** Authenticate via the **Client Portal Gateway** (manual browser login
+ 2FA) and a `/tickle` keep-alive. Keep the `AuthPort` abstraction, but ship only
the Gateway adapter.

**Why.** The original target was headless OAuth for fully autonomous scheduling.
Research against official IBKR sources confirmed that **Web API OAuth is not
available to retail/individual accounts** — OAuth 1.0a is institutional /
registered-third-party only (a multi-week Compliance process); individual OAuth 2.0
is "under consideration, no ETA". So the only real path for retail is the gateway.

**Confirmed operational implications.**
- Session expires in ~6 min without `/tickle` (tickle every ~60s).
- Max session length ~24h, with a reset around midnight.
- A daily maintenance window (~01:00 local) drops the session — schedule jobs
  around it.
- One brokerage session per username → use a dedicated username for the bot.
- `GET /iserver/accounts` is mandatory before placing orders.
- The live account must be open/funded/IBKR Pro even to use only the paper account.

**Alternatives considered.** Headless OAuth (the original target — now unavailable
for retail); Gateway-only was the fallback that became the answer.

---

## ADR-005 — Scope = paper-first with a hard live lock

**Decision.** Operate against the paper account by default. `live` exists but only
behind an explicit flag, with dry-run as the default, a per-order value limit, and
a trading-hours check.

**Why.** Safety. Real money should never be one typo away. The guards live in a
`GuardedBroker` decorator on the path of every order.

**Alternatives considered.** Read-only/simulation first (too slow to iterate);
paper-only with no path to live (doesn't meet the end goal).

---

## ADR-006 — Live validation confirmed the core bet (CPAPI + cashQty)

**Decision / finding.** Validated end-to-end against a real IBKR account.

**Why it matters.** Auth/connected OK; balance, quotes and positions read
correctly. The account flags confirmed the key requirement: `supportsCashQty:true`
and `supportsFractions:true` (and `liteUser:false` → Pro). The bet on CPAPI for
fractional was correct and executable.

**Operational findings.** The gateway's 2023 launcher build is **not** a problem
(runtime `serverVersion` reports a current build that connects to the live
backend). Login only unblocked with: a clean gateway restart + an incognito tab +
no competing sessions. On the test machine the paper account would not connect
(`ssodh` 500) while the real account did — the opposite of the expectation, so the
real account was used to validate.

---

## ADR-007 — `cashQty` is buy-only; fractional sells go by share quantity

**Decision / finding.** A live round-trip revealed that IBKR **rejects `cashQty` on
sell orders**: `"Cash order quantity can not be set for sell order"` (a system
rejection). Dollar-amount fractional sizing works on **buys only**. To
close/reduce a fractional position you must send a **fractional `quantity`** (e.g.
0.0066 shares) — which is how the sell finally executed.

**Code implication.** `OrderRequest.quantity` had to change from `int` to `Decimal`
so the agent can close a fractional position. A `close_position` primitive reads
the exact position size and sells all of it (avoiding both `cashQty` and the
oversell warning).

**Decision on warning `o2137`.** The sell warning `o2137` ("closing order quantity
is greater than your current position") is **deliberately kept out** of the global
allow-list — auto-confirming "sell more than you hold" has dangerous semantics
(oversell/short) and is exactly what the guard should block. The correct path is to
sell the exact quantity, which never triggers the warning.

**Reply messageIds mapped live (BUY MKT + cashQty allow-list).** `o354`, `o10164`
(Market Order Confirmation), `o10223` (Mandatory Cap Price), `o10151` (cash
quantity disclaimer), `o10153` (Cash Quantity Order Confirmation) — all marked
`isSuppressible:true` / "Accept and Continue" by the API.

---

## ADR-008 — Positions endpoint is eventually-consistent; close right after a buy is best-effort

**Decision / finding.** The `/portfolio/{account}/positions` endpoint is cached and
eventually-consistent: right after a fill it can keep reporting `0.0` for tens of
seconds (observed live). Authoritative state comes from the ledger
(`stockmarketvalue`/`cashbalance`), not from positions alone.

**Code implication.** `get_positions` filters out zero-quantity rows (the stale
"phantom" entries); `close_position` invalidates the cache before reading and, when
no position is found, returns an honest message about the lag rather than a false
"nothing to close".

---

## ADR-009 — Safety and auditability are layered, not a single check

**Decision.** Trading safety is not one gate but a stack, all on the path of every
order in `GuardedBroker`, plus a preview step and an audit trail:

- **Preview before commit.** `preview_order` (IBKR `whatif`) estimates margin impact,
  commission and warnings *without sending*, so the agent can reason about cost first.
- **Per-order limit** (`MAX_ORDER_VALUE`) and an optional **cumulative daily cap**
  (`MAX_DAILY_VALUE`) — many small buys can't sneak past a per-order limit.
- **Duplicate guard.** An identical order within `DUPLICATE_WINDOW_SECONDS` is
  rejected, so a timeout-and-retry can't double-buy.
- **Symbol allow/deny list.** Restricts the universe the agent can touch.
- **Audit log.** Every attempt (sent, dry-run, blocked) is appended to a local JSONL
  journal — it answers "what did my agent do?" and is the source of truth for the
  daily cap and the duplicate guard.

**Why.** This is real money driven by an autonomous agent. Defence in depth (preview +
multiple independent guards + a tamper-evident-ish append-only log) is what makes that
trustworthy, and it costs little: the journal is dependency-free JSONL, the guards are
cheap checks, and everything is testable offline.

**Notes.** The daily cap and duplicate guard are computed from the journal, so they're
naturally consistent with what was actually recorded. The `whatif` parse is validated
against a live retail response: IBKR returns money as unit-suffixed strings
(`"2.02 USD"`), warnings as `"<code>/<html>"` entries, and — for a fractional cash
order — leaves the margin blocks null while reporting the available-funds impact in
`data` rows. The parser handles those shapes and the full `raw` payload is always
returned alongside the structured fields.
