"""Configuração central, carregada de variáveis de ambiente / `.env`."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.models import TradingMode

# Caminho absoluto do .env na raiz do projeto, para o servidor achá-lo independente do
# diretório de onde for iniciado (ex.: quando o Claude Code lança o MCP).
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # Conexão CPAPI
    ibkr_api_base_url: str = "https://localhost:5000/v1/api"
    ibkr_account_id: str = ""
    ibkr_trading_mode: TradingMode = TradingMode.PAPER

    # Segurança
    trading_allow_live: bool = False
    trading_dry_run: bool = True
    max_order_value: Decimal = Decimal("100")

    # Sessão / rede
    tickle_interval_seconds: int = 60
    request_timeout_seconds: float = 15.0

    # Mercado
    market_timezone: str = "America/New_York"
    market_open_time: str = "09:30"
    market_close_time: str = "16:00"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
