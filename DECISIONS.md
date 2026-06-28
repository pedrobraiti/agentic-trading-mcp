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
- **Per-order spend limit** (`MAX_ORDER_VALUE`) and an optional **cumulative daily cap**
  (`MAX_DAILY_VALUE`) — many small buys can't sneak past a per-order limit. Both gate
  *spending*, so they apply to **buys only**: exits (sells, closes, stop-losses) reduce
  exposure and are never value-blocked, otherwise a position larger than the limit could
  not be closed or protected.
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

---

## ADR-010 — Close the agent's loop: order status, limit orders, in-server keep-alive

**Context.** Three rough edges made an agentic flow harder than it should be: after a
buy the agent had no way to confirm a *fill* (positions are eventually-consistent);
only market orders were possible (no price target or stop); and a long-lived MCP
process could silently lose its brokerage session because the keep-alive was a separate
manual command.

**Decision.**

- **`order_status(order_id)`** reads `/iserver/account/order/status/{id}` and returns
  the state, filled quantity and average price — so the agent confirms a fill directly
  instead of polling positions. Validated live: the endpoint uses `symbol`, `side`
  (`"B"`/`"S"`), `cum_fill` and `order_status`; the full `raw` is kept regardless.
- **LIMIT orders.** `buy`/`sell`/`preview_order` accept an optional `limit_price`
  (market by default). LIMIT requires `quantity` — `cashQty` is market-only, so a
  `limit_price` + `cash_amount` combination is rejected up front. Validated live: a
  deliberate limit placed away from the market triggers IBKR's `o163` ("price exceeds
  the percentage constraint") confirmation, now allow-listed alongside the existing
  precautions. Any *other*, unmapped warning still **blocks** the order rather than
  auto-confirming — consistent with the safety design.
- **In-server keep-alive.** The MCP server runs a background `/tickle` on its lifespan,
  so interactive use needs no separate process. The standalone `ibkr-keepalive` stays
  for headless/scheduled use. Neither can log in for a retail account — they only
  tickle and alert.

**Why.** These are the difference between "the plumbing exists" and "an agent trades
without friction": confirm the fill, set a price, and don't drop the session mid-session
— without weakening any of the ADR-009 guards (they all still sit on the order path).

---

## ADR-011 — Stop and bracket orders, with risk attached to the entry

**Context.** Market and limit orders let an agent *enter* a position, but not protect
one. A stop-loss and a take-profit are the basic risk tools; a bracket binds them to an
entry so the protection is in place the moment the entry fills.

**Decision.**

- **`stop_order`** places a STOP (market-on-trigger) or STOP-LIMIT. CPAPI's price-field
  convention is non-obvious and was confirmed live: a plain STOP carries its trigger in
  `price` (sending `auxPrice` is rejected with "Invalid order price fields"); a
  STOP-LIMIT uses `price` for the limit and `auxPrice` for the trigger.
- **`bracket_order`** submits the entry plus two children — a take-profit limit and a
  stop-loss — as one payload, with each child's `parentId` set to the entry's `cOID`.
  IBKR links them into an OCA group (confirmed live: the children come back with a
  shared `ocaGroupId`), so when one exit fills the other is cancelled. The entry must be
  sized by `quantity` (not `cashQty`): the exits need a definite share count, which a
  dollar-amount entry can't provide until it fills.
- **Guarding the entry.** A bracket runs through the same `GuardedBroker` checks as any
  order, applied to the *entry* (the leg that spends money); the exits ride along in the
  same submission. The guard logic is shared between `place_order` and `place_bracket`.

**Why.** Stop/bracket are the natural completion of the execution surface — an agent can
now enter *and* define its exit in one call. The CPAPI field quirks here are exactly the
kind of thing that only surfaces against the real API, so both paths were validated live
(a non-triggering stop and a non-filling bracket, then cancelled); the unit tests pin the
exact order bodies so regressions can't slip the field convention.

---

## ADR-012 — Trailing stops, and a 503 that doesn't double-submit

**Context.** Two gaps showed up in real use. (1) A trailing stop — a stop that follows
the price to lock in gains — was missing. (2) The gateway sometimes answers an order
POST with a transient **HTTP 503 even though the order already landed**; retrying blindly
would place the order twice (real money).

**Decision.**

- **`trailing_stop`** sends a CPAPI `TRAIL` order with `trailingAmt` + `trailingType`
  (`"amt"` for US$, `"%"` for percent). Validated live; like the other order variants it
  raises a benign confirmation — `o10152` "Stop Variant Order Confirmation" — now
  allow-listed, so stop / stop-limit / trailing orders aren't blocked.
- **Idempotent order POST.** On a 503, before retrying we look the order up by its client
  id (`cOID`, returned as the live order's `order_ref`); if it's already there we return
  that instead of sending again. Only a genuine miss is retried. This sits in front of
  both `place_order` and `place_bracket`.
- **Position precision.** Money fields (market price, average cost, market value,
  unrealized P&L) are rounded to cents like the balances; the fractional **quantity**
  stays exact.

**Why.** The 503 case is the kind of silent foot-gun that only an autonomous trader hits
(it retries faster than a human), and a duplicate order is the worst outcome — so the
fix is idempotency, not just a retry count. Trailing stops complete the protective-order
set. Both were validated against the live API, where the field and warning quirks actually live.

---

## ADR-013 — Fail-closed on identity uncertainty; multi-agent audit and what we deliberately did NOT do

**Context.** A live session surfaced a grotesque-but-invisible failure: the agent
couldn't reliably tell whether it was on a paper or a real account, because the
money-lock trusted the cosmetic `IBKR_TRADING_MODE` label instead of IBKR's real
`isPaper`. That prompted a deeper, **multi-agent adversarial review** (six focused
subagents + a macro pass) that found a family of the same shape: things that are
*uncertain* (account identity, instrument identity, whether a sent order landed) were
failing **open**.

**Decision — one principle: uncertainty fails closed.**

- **Account is ground truth, not the label.** `account_info()` coerces `isPaper`
  robustly (bool/str/int) and only marks paper on a known `DU` prefix; anything else is
  `None` (unknown). The guard refuses to trade when the configured account ≠ the
  logged-in one, when a real account isn't armed with `TRADING_ALLOW_LIVE`, when the
  label disagrees with reality, **or when paper-vs-live can't be confirmed at all**.
- **Exits are never trapped.** The value-cap notional is computed only for BUYs (pricing
  a SELL could raise on a missing quote and block an exit); the naked-short check skips
  when holdings can't be confirmed and refreshes positions first.
- **Brackets can't self-liquidate.** `BracketRequest` validates take-profit/stop-loss
  sit on the correct sides; the guard also rejects a stop-loss already on the wrong side
  of the live market.
- **Idempotency by "sent", not by order_id.** A dispatched-but-unconfirmed order
  (timeout/503) is journaled as `sent`, so the duplicate guard catches a retry that may
  have already filled. A 503 whose follow-up lookup fails is reported as *indeterminate*
  rather than blindly resent. Cancels report `CANCELLED` only when the gateway confirms.
- **Misconfig is loud.** A safety-prefixed `.env` key that maps to no setting (a typo
  that would silently disable a cap/list) is warned about at startup. `_pick_us_conid`
  no longer falls back to a foreign listing; field-31 state prefixes ("C…"/"H…") are
  parsed.

**Deliberately NOT done (judged filler for this project — recorded so it's a choice, not an oversight).**

- **A lock around guard-read→place→journal-write (TOCTOU).** The window only opens under
  *concurrent* order calls; a single agent issues tool calls serially, so it doesn't
  occur in practice. Revisit if multiple concurrent callers are ever introduced.
- **`fsync`/atomic journal append.** Guards against a torn line from a crash mid-write of
  a tiny JSON record — rare, and a corrupt line is already skipped-and-logged.
- **Gating on the `competing` session flag, per-account base currency, and assorted
  LOW mislabels.** Negligible for a single-session USD account.
- **Sending order numbers as `Decimal` instead of `float` (a reviewer's "MEDIUM").**
  Investigated and found to be a **false alarm**: `json.dumps(float(Decimal("100.005")))`
  is `"100.005"` (Python uses the shortest round-trip repr), so there is no precision
  loss on the wire. No change made.

**Why.** Robustness is about the *direction* a thing fails, not the count of checks.
Binding every identity/idempotency decision to fail closed removes the whole class of
"silently did the wrong thing." The skipped items are documented here so a future reader
sees they were weighed and declined, not missed — and so the false alarm isn't
re-litigated.

---

## ADR-014 — A second venue (crypto/CCXT) as a sibling MCP over a shared core

**Decision.** Evolve the repo into a **monorepo of two execution servers** —
`ibkr` (unchanged) and a new `crypto` server over **CCXT** — sharing a `trading_core`
package (domain models, ports, trade journal, and a now venue-agnostic `GuardedBroker`).
The two are **separate MCP processes** with their own login and tools; they only share
code. Spot-only by default.

**Why.** The structural weakness of the setup was never the code — it was **IBKR retail
auth**: no OAuth, so the session needs a local gateway, a manual browser login, a
keep-alive tickle, and a daily teardown. For an agent meant to run on its own, that's
fragile. Crypto exchanges remove exactly that: a **persistent API key** (no gateway/
browser/tickle), a **24/7** market, and CCXT unifies ~100 venues behind one interface that
slots into the existing hexagonal port. Reusing one safety core means the guards are
written once and both venues inherit them.

**How it stays generic.** The guard stopped assuming IBKR: position sizing for exits moved
from a conid lookup to a `held_quantity(symbol)` port, the paper/live account check became
venue-neutral (wording parameterized), and each venue declares a `Capabilities` contract
(market-hours model, shorting, buy-by-value, quote currency). Buy-by-value — IBKR's
`cashQty` — maps to CCXT's `createMarketBuyOrderWithCost` with a price-based fallback.
Extraction was behavior-preserving: the original IBKR suite stays green and old import
paths keep working via shims.

**Safety choices specific to crypto.** Sandbox (exchange testnet) is paper-first, but not
every exchange has one, so dry-run remains the real backstop. The real-money arm is a
**separate** `CRYPTO_ALLOW_LIVE` gate (arming IBKR must not arm crypto). Spot-only is a hard
default (`CRYPTO_ALLOW_MARGIN=false`); enabling it is what allows shorting.

**Alternatives considered.** A separate repo for crypto (rejected — the shared `trading_core`
only stays simple if the servers live together); putting crypto tools in the research MCP
(wrong layer — research ≠ execution); margin/derivatives now (out of scope — spot-only);
`ccxt.pro`/websockets (REST is enough to start).

---

## ADR-015 — Daily-cap default stays off but warns loudly; `inactive` counts conservatively

**Context.** Two LOW-severity backlog items on the spend backstops. (1) `MAX_DAILY_VALUE`
defaults to None, so out of the box the only dollar backstop is the per-order
`MAX_ORDER_VALUE` — a loop of many sub-cap buys has no cumulative daily ceiling. (2) The
journal's `spent_today`/`has_recent_duplicate` exclude only `rejected`/`cancelled`, so an
`inactive` order counts toward both the daily cap and the duplicate window.

**Decision — keep both defaults, make the gap visible.**

- **Daily cap: no silent default, loud warning instead.** Flipping `MAX_DAILY_VALUE` to a
  non-null number by default would silently change behavior for existing setups (and any
  guessed number is wrong for someone). Instead, when it's unset **and** live trading is
  armed (`TRADING_ALLOW_LIVE` for IBKR, `CRYPTO_ALLOW_LIVE` for crypto), `get_settings`
  emits a loud startup warning — mirroring the existing typo-warning — telling the operator
  that only the per-order cap applies. Documented in `.env.example` and the README.
- **`inactive` counts toward caps on purpose.** CPAPI uses `inactive` for *both* a
  dead/rejected order and one parked until the open — and (per the broker's status map) a
  genuinely rejected order also arrives as `inactive`, since CPAPI doesn't emit a `Rejected`
  string. There is **no reliable sub-reason offline** to tell dead from parked, so rather
  than guess we keep `inactive` counting toward spend and the duplicate window. The
  direction is fail-safe: it may over-block a retry, but it never lets real spend slip the
  cap. If a future live gateway exposes a trustworthy sub-status, this can be refined.

**Why.** Both choices follow ADR-013's principle: uncertainty fails closed, and a
behavior gap is surfaced loudly rather than papered over with a default that could
surprise someone or a distinction we can't actually make.

## ADR-016 — A reconciled cancel frees daily budget; idempotency is single-writer

**Context.** Two LOW findings from the unattended-safety adversarial pass on the cOID
idempotency / spend-cap work (0.5.0).

**Decision 1 — a reconcile that confirms *no money moved* frees the daily budget.**
A timed-out dispatch counts toward `spent_today` while in flight (fail-safe). Previously,
resolving it via `reconcile_pending` left the phantom spend on the books forever — even when
the reconcile confirmed the order was `cancelled`/`rejected` and moved no money — which could
refuse a later *legitimate* order until the daily reset. That is an annoyance, not a safety
feature: the safe direction is to over-block *uncertain* spend, not *confirmed-zero* spend.
`spent_today` now honors a terminal reconcile status — a reconciled `cancelled`/`rejected`
frees the budget, while `unknown`/`filled`/`inactive` keep counting (still fail-safe). This
also keeps the system from paternalistically blocking a deliberate order for a non-reason.

**Decision 2 — the idempotency guard is single-writer; run one Valet server per account.**
The persistent cOID guard (`has_unresolved_dispatch`) reads the journal before each order, so
a crashed/timed-out dispatch that is *relaunched or retried sequentially* is correctly blocked
across processes — the realistic unattended threat. It does **not** add a cross-process file
lock, so two genuinely *simultaneous* OS processes sharing one journal + account could both
pass the check before either writes its intent (a TOCTOU race), and the random per-attempt
cOID means the venue can't dedupe either. We do not add OS-specific file locking to the
money path for an unusual deployment; instead the operating contract is: **run a single
persistent Valet server per account for unattended use.** Closing the simultaneous-process
case venue-side would mean a *deterministic* cOID (so the broker rejects the duplicate) — that
belongs with the P8 live-gateway validation of IBKR's cOID dedup, not a blind offline change.

**Why.** Same ADR-013 spine: fix the accounting so confirmed-zero spend doesn't linger
(correctness + no false refusal), and make the remaining race an explicit, documented
deployment constraint rather than papering it with platform-specific locking we can't test
offline in the highest-stakes path.
