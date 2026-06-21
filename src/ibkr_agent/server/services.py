"""Composition root: monta os adapters concretos a partir das Settings.

É aqui que a injeção de dependência acontece — o resto do código depende só das
portas. Trocar CPAPI por outro adapter no futuro é mexer só neste arquivo.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.cpapi import CpapiBroker, CpapiClient, CpapiMarketData, GatewayAuth
from ..config import Settings, get_settings
from ..safety import GuardedBroker, is_market_open_now


@dataclass
class Services:
    settings: Settings
    client: CpapiClient
    auth: GatewayAuth
    market_data: CpapiMarketData
    broker: GuardedBroker

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
    )
    return Services(
        settings=settings,
        client=client,
        auth=auth,
        market_data=market_data,
        broker=guarded,
    )
