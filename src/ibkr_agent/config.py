"""Central configuration, loaded from environment variables / `.env`."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.models import TradingMode

# Absolute path to the .env at the project root, so the server can find it regardless of
# the directory it is started from (e.g. when Claude Code launches the MCP).
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # CPAPI connection
    ibkr_api_base_url: str = "https://localhost:5000/v1/api"
    ibkr_account_id: str = ""
    ibkr_trading_mode: TradingMode = TradingMode.PAPER

    # Safety
    trading_allow_live: bool = False
    trading_dry_run: bool = True
    max_order_value: Decimal = Decimal("100")
    # Cumulative spend across all buys in a day (market tz). None = no daily cap.
    max_daily_value: Decimal | None = None
    # Reject an identical order (symbol/side/size) placed within this window. 0 = off.
    duplicate_window_seconds: float = 5.0

    # Audit
    trade_journal_path: str = "logs/trades.jsonl"

    # Session / network
    tickle_interval_seconds: int = 60
    request_timeout_seconds: float = 15.0

    # Market
    market_timezone: str = "America/New_York"
    market_open_time: str = "09:30"
    market_close_time: str = "16:00"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
