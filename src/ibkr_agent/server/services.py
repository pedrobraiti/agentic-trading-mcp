"""Composition root: assembles the concrete adapters from the Settings.

This is where dependency injection happens — the rest of the code depends only on the
ports. Swapping CPAPI for another adapter in the future means touching only this file.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.cpapi import CpapiBroker, CpapiClient, CpapiMarketData, GatewayAuth
from ..config import Settings, get_settings
from ..journal import TradeJournal
from ..safety import GuardedBroker, is_market_open_now


@dataclass
class Services:
    settings: Settings
    client: CpapiClient
    auth: GatewayAuth
    market_data: CpapiMarketData
    broker: GuardedBroker
    journal: TradeJournal

    def market_is_open(self) -> bool:
        return is_market_open_now(
            self.settings.market_timezone,
            self.settings.market_open_time,
            self.settings.market_close_time,
        )


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    client = CpapiClient(settings.ibkr_api_base_url, timeout=settings.request_timeout_seconds)
    auth = GatewayAuth(client)
    market_data = CpapiMarketData(client, settings.ibkr_account_id)
    raw_broker = CpapiBroker(client, settings.ibkr_account_id, market_data.resolve_conid)
    journal = TradeJournal(
        settings.trade_journal_path, market_timezone=settings.market_timezone
    )
    guarded = GuardedBroker(
        raw_broker,
        market_data,
        mode=settings.ibkr_trading_mode,
        allow_live=settings.trading_allow_live,
        dry_run=settings.trading_dry_run,
        max_order_value=settings.max_order_value,
        require_market_open=True,
        is_market_open=lambda: is_market_open_now(
            settings.market_timezone, settings.market_open_time, settings.market_close_time
        ),
        journal=journal,
        max_daily_value=settings.max_daily_value,
        duplicate_window_seconds=settings.duplicate_window_seconds,
    )
    return Services(
        settings=settings,
        client=client,
        auth=auth,
        market_data=market_data,
        broker=guarded,
        journal=journal,
    )
