"""Composition root for the crypto server: assembles the CCXT adapter + generic guard.

Mirrors ``ibkr_agent.server.services`` but for a spot crypto venue: no session keeper
(API keys don't expire like the IBKR gateway), a venue-specific live gate, and the
ALWAYS_OPEN market-hours model.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_core.capabilities import Capabilities, crypto_capabilities
from trading_core.journal import TradeJournal
from trading_core.safety import GuardedBroker

from ..adapters.ccxt import CcxtBroker, CcxtClient, CcxtMarketData
from ..config import CryptoSettings, get_settings


async def _probe_account_info(client: CcxtClient, settings: CryptoSettings) -> dict:
    """Probe the keys (raises if they don't authenticate) and report venue identity.

    ``is_paper`` reflects sandbox vs live — but, unlike IBKR's ``isPaper``, CCXT exposes no
    authenticated endpoint that confirms a key pair is testnet vs mainnet, so this verdict is
    DERIVED from the configured ``CRYPTO_TRADING_MODE``, not venue-confirmed. We flag that with
    ``identity_verified: False`` so a silent sandbox/live KEY mismatch (live keys under a
    sandbox label would otherwise read as "PAPER") can't rest on the dry-run flag alone — the
    guard then requires the explicit ``CRYPTO_ALLOW_LIVE`` ack before any real order. Shared by
    the guard's account provider and ``session_status`` so both report the exact same thing.
    """
    await client.exchange.fetch_balance()  # raises if the keys don't authenticate
    is_paper = settings.is_sandbox
    return {
        "account_id": settings.crypto_exchange,
        "is_paper": is_paper,
        "account_type": "PAPER" if is_paper else "LIVE",
        "identity_verified": False,
    }


@dataclass
class Services:
    settings: CryptoSettings
    client: CcxtClient
    market_data: CcxtMarketData
    broker: GuardedBroker
    journal: TradeJournal
    capabilities: Capabilities

    async def account_info(self) -> dict:
        """Identity for ``session_status`` — the same probe the guard runs before an order."""
        return await _probe_account_info(self.client, self.settings)


def _symbols(raw: str) -> frozenset[str]:
    return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())


def build_services(settings: CryptoSettings | None = None) -> Services:
    settings = settings or get_settings()
    client = CcxtClient(
        settings.crypto_exchange,
        api_key=settings.crypto_api_key,
        api_secret=settings.crypto_api_secret,
        password=settings.crypto_api_password,
        sandbox=settings.is_sandbox,
        quote_currency=settings.crypto_quote_ccy,
    )
    market_data = CcxtMarketData(client)
    # 24/7 venue → account the daily-spend cap by UTC days, not the IBKR NY trading day.
    journal = TradeJournal(settings.crypto_trade_journal_path, market_timezone="UTC")
    capabilities = crypto_capabilities(
        settings.crypto_quote_ccy, allow_margin=settings.crypto_allow_margin
    )

    guarded = GuardedBroker(
        CcxtBroker(client),
        market_data,
        mode=settings.trading_mode,
        allow_live=settings.crypto_allow_live,
        dry_run=settings.crypto_dry_run,
        max_order_value=settings.max_order_value,
        require_market_open=capabilities.requires_market_open,  # ALWAYS_OPEN → False
        is_market_open=lambda: True,
        journal=journal,
        max_daily_value=settings.max_daily_value,
        duplicate_window_seconds=settings.duplicate_window_seconds,
        symbol_allowlist=_symbols(settings.symbol_allowlist),
        symbol_denylist=_symbols(settings.symbol_denylist),
        account_info_provider=lambda: _probe_account_info(client, settings),
        configured_account_id="",  # no fixed account id to match on a crypto exchange
        allow_short=capabilities.supports_shorting,  # spot-only ⇒ no shorting by default
        venue=f"the {settings.crypto_exchange} exchange",
        live_env_var="CRYPTO_ALLOW_LIVE",
        mode_env_var="CRYPTO_TRADING_MODE",
        account_env_var="CRYPTO_API_KEY",
    )
    return Services(
        settings=settings,
        client=client,
        market_data=market_data,
        broker=guarded,
        journal=journal,
        capabilities=capabilities,
    )
