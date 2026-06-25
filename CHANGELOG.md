# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **`session_status` and `portfolio` report `account_type`** (`"LIVE"`/`"PAPER"`)
  straight from IBKR's `isPaper` — the ground truth, independent of the cosmetic
  `IBKR_TRADING_MODE` label. A LIVE account also returns an explicit `warning`, so the
  agent can never mistake a real-money account for paper.
- **The safety guard now binds the money-lock to the real account, not the label.**
  Before sending, `GuardedBroker` checks IBKR's `isPaper` and the logged-in account id
  and **fails closed** when: the configured `IBKR_ACCOUNT_ID` ≠ the logged-in account; a
  LIVE (real-money) account isn't armed with `TRADING_ALLOW_LIVE=true`; or the
  `IBKR_TRADING_MODE` label disagrees with reality. A mislabelled setup can no longer
  quietly move real money.
- **Naked-short guard:** a SELL larger than the held position is blocked (it would open a
  short) unless `TRADING_ALLOW_SHORT=true`. Exits are never trapped — if holdings can't be
  read, the check is skipped.
- **Inverted-stop guard:** a STOP already on the wrong side of the market (a SELL stop
  at/above, or a BUY stop at/below, the current price) is rejected — it would trigger
  instantly, almost always a fat-finger.

### Changed
- **Trade journal reads survive a corrupt line** (skipped and logged) instead of raising —
  a single bad line no longer bricks the daily-spend cap, the duplicate guard, or trading
  itself. Journaling failures are now logged rather than silently swallowed.

### Hardened (multi-agent audit — see ADR-013)
- **Uncertainty fails closed.** Refuse to trade when paper-vs-live can't be confirmed from
  IBKR (`isPaper` missing/unparseable), when the logged-in account is unreadable, or on a
  non-`U`/non-`DU` account whose status is unknown — instead of guessing "paper".
- **Exits are never trapped.** The value-cap quote is fetched only for BUYs (a missing
  price no longer blocks a SELL); the naked-short check skips when holdings can't be
  confirmed and refreshes positions first.
- **Brackets can't self-liquidate.** `BracketRequest` validates take-profit/stop-loss
  ordering; the guard rejects a stop-loss on the wrong side of the live market.
- **Idempotency keyed on "sent".** A dispatched-but-unconfirmed order (timeout/503) is
  journaled as `sent`, so a retry is caught as a duplicate; a 503 whose lookup fails is
  reported indeterminate, not resent. `cancel_order` reports `CANCELLED` only when the
  gateway confirms it (otherwise `pending`).
- **Misconfig is loud / data is cleaner.** A typo'd safety-prefixed `.env` key is warned
  about at startup; `cash_qty` is pinned to MARKET BUY; conid resolution no longer falls
  back to a foreign listing; field-31 state-prefixed prices (`"C…"`/`"H…"`) are parsed.
- **`TRADING_ALLOW_SHORT`** (default `false`) gates whether a SELL may exceed the held
  position.

### Hardened — third audit pass (new lenses: diff, lifecycle/concurrency, API-assumptions)
- **`close_position` won't sell a position twice during portfolio lag.** A repeat close of
  the same contract within a cooldown is refused (pointing to `order_status`) — so the
  doc-recommended "wait and retry" can't open an unintended short.
- **Order placement is serialized** (an `asyncio.Lock` around check→dispatch→journal), so
  two parallel tool calls can't both slip past the daily-spend cap or the duplicate guard.
- **`Inactive` order status is mapped** (CPAPI's real string for a dead/parked order;
  `Rejected` is TWS-only) and treated as terminal, so `wait_for_fill` stops instead of
  polling to the timeout on an order that's already done. `PendingCancel`/`ApiCancelled`
  mapped too.
- **Bracket exit checks respect a LIMIT entry's fill price** — a valid "buy the dip"
  bracket is no longer wrongly blocked by comparing exits to the live market.
- **`cancel_order` no longer raises on a cancel-confirmation question** — it reports
  pending with the message instead of declining it through the order allow-list.

### Hardened — second audit pass (run-proven multi-agent review)
- **Value cap no longer bypassable by a zero/negative price** — a non-positive quote is
  treated like a missing one (fail closed), so it can't make the notional 0 and slip a
  large BUY past `MAX_ORDER_VALUE`.
- **Brackets also check the take-profit vs the live market** (not just the stop-loss), so
  a take-profit on the wrong side can't fill instantly and round-trip the position.
- **A transient failure reading the account no longer traps an exit** — once an identity
  is confirmed, a momentary `/iserver/accounts` blip falls back to it instead of blocking
  (still fails closed when nothing was ever confirmed).
- **Daily-spend cap counts dispatched-but-unconfirmed buys** (which may have spent money)
  and **excludes rejected/cancelled** ones (which didn't).
- **Duplicate guard keys on `order_type` too and ignores rejected/cancelled attempts** —
  a resting STOP no longer blocks a panic MARKET exit, and a rejected order doesn't block
  its corrected retry.
- Smaller fixes: non-finite (`NaN`/`Inf`) money values dropped instead of crashing;
  `isPaper` only accepts `0`/`1` as an int (else unknown → fail closed); quote warmup waits
  for a real price (keeping bid/ask); `cancel_order` parses list-shaped/confirmation
  responses; the confirmation loop declines a leftover question on abort; conid resolution
  falls back to a USD listing when `isUS` is absent (never a foreign one).

### Docs
- Troubleshoot `ERR_CONNECTION_REFUSED` (the gateway simply isn't running) distinctly
  from the "logged in but nothing happens" case.
- Clarify that `IBKR_TRADING_MODE` is a label, not the account selector.

## [0.3.0] - 2026-06-23

### Added
- **`trailing_stop`** — a stop that follows the price by a US$ amount or a %. CPAPI
  fields (`trailingAmt`/`trailingType`) and the `o10152` "Stop Variant" confirmation
  were validated live; `o10152` is now allow-listed so stop/stop-limit/trailing orders
  aren't blocked.

### Changed
- **Order placement survives a transient 503.** The gateway can answer an order POST
  with a 503 *after the order already landed*; placement now looks the order up by its
  client id (cOID) before retrying, so it never double-submits.
- **Position money fields are rounded to cents** (market price, average cost, market
  value, unrealized P&L); the fractional **quantity** stays exact.

## [0.2.2] - 2026-06-23

### Added
- **`get_quotes(symbols)`** — quote a whole watchlist in one snapshot call instead of
  one round-trip per symbol. Live-validated.
- **`wait_for_fill(order_id, timeout_seconds)`** — poll an order until it fills (or is
  cancelled/rejected), closing the confirm-the-fill loop so the agent doesn't have to
  orchestrate the retry. Bounded (timeout capped at 120s).

### Docs
- `CLAUDE.md`'s day-to-day tool list was stale (10 tools); now lists all 18.

## [0.2.1] - 2026-06-23

### Fixed
- **The value cap no longer blocks exits.** `MAX_ORDER_VALUE` is a spend limit, so it
  now applies to BUYS only — sells, `close_position`, and stop-losses larger than the
  limit are allowed, otherwise a position bigger than the cap could not be closed or
  protected.

## [0.2.0] - 2026-06-23

A loop-closing release: the agent can now confirm fills, set prices, attach risk, and
keep its own session warm. Every new order path was validated live.

### Added
- **`order_status(order_id)`** tool — confirm an order's state, filled quantity and
  average price after `buy`/`sell` (positions lag right after a trade).
- **LIMIT orders** — `buy`/`sell`/`preview_order` accept an optional `limit_price`
  (market by default; LIMIT requires `quantity`, since `cashQty` is market-only).
  Live-validated; IBKR's `o163` percentage-constraint confirmation is allow-listed so
  deliberate away-from-market limits aren't spuriously blocked.
- **`stop_order`** — STOP (stop-loss) and STOP-LIMIT orders. Live-validated; CPAPI
  carries a plain STOP's trigger in `price` (not `auxPrice`).
- **`bracket_order`** — an entry with attached take-profit + stop-loss exits (OCO via
  `parentId`/`ocaGroupId`). Guarded on the entry; live-validated structurally.
- **In-server keep-alive** — the MCP server now runs a background `/tickle` on its
  lifespan, so interactive use no longer needs the standalone `ibkr-keepalive`.
- GitHub issue/PR templates.

### Fixed
- **`whatif`/`preview_order` parsing** validated against a live response: money fields
  arrive as unit-suffixed strings (`"2.02 USD"`), warnings come from `warns`, and a
  fractional cash order reports available-funds impact (now exposed as
  `available_funds_before`/`after`) instead of the null margin blocks.

## [0.1.0] - 2026-06-22

First working release, validated live against a real IBKR account.

### Added
- **MCP server** (FastMCP) with 10 tools: `session_status`, `market_status`,
  `get_quote`, `account_summary`, `positions`, `buy`, `sell`, `close_position`,
  `cancel_order`, `open_orders`.
- **Fractional buys** by dollar amount (`cashQty`) and **fractional sells/closes**
  by share quantity (incl. `close_position`, which reads the exact position size).
- **Hexagonal** architecture (domain / adapters / safety / server) over the
  Interactive Brokers Client Portal API (REST).
- **Safety guards**: paper-first, dry-run by default, live lock, per-order value
  limit, regular trading hours (RTH) with **NYSE holidays**, and an allow-list for
  confirmation warnings (with an automatic *decline* when an unknown warning blocks
  the order).
- **Keep-alive** session loop with a reauth alert (`ibkr-keepalive`).
- **Healthcheck** for connection/account (`ibkr-healthcheck`).

[0.3.0]: https://github.com/pedrobraiti/mcp-ibkr-agent/releases/tag/v0.3.0
[0.2.2]: https://github.com/pedrobraiti/mcp-ibkr-agent/releases/tag/v0.2.2
[0.2.1]: https://github.com/pedrobraiti/mcp-ibkr-agent/releases/tag/v0.2.1
[0.2.0]: https://github.com/pedrobraiti/mcp-ibkr-agent/releases/tag/v0.2.0
[0.1.0]: https://github.com/pedrobraiti/mcp-ibkr-agent/releases/tag/v0.1.0
