# Security Policy

This project places **real orders** in a brokerage account (Interactive Brokers).
Treat it with the corresponding care.

## Secrets and credentials
- **Never** commit the `.env` file (it is already in `.gitignore`). It holds the
  account id and sensitive configuration. The committed file is `.env.example`
  (keys only, no values).
- There are no IBKR API keys in the code: authentication is a **manual login** to
  the Client Portal Gateway (a local session). Do not paste session tokens/cookies
  into issues.
- Before opening an issue or PR, make sure no pasted log contains a real account
  id, balances, or order identifiers.

## Code safety defaults
- `paper`/dry-run by default; `live` requires an explicit `TRADING_ALLOW_LIVE=true`.
- Per-order value limit (`MAX_ORDER_VALUE`) and a trading-hours (RTH) check.
- Unknown confirmation warnings **block** the order (they are not auto-confirmed).

## Reporting a vulnerability
Instead of opening a public issue, use GitHub
[Security Advisories](https://github.com/pedrobraiti/agentic-trading-mcp/security/advisories/new)
(private disclosure). Describe the impact and how to reproduce it. I'll respond as
soon as I can.
