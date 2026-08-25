"""Kaupo configuration.

All settings come from environment variables (prefixed ``KAUPO_``) with
sensible local-development defaults. No secrets are stored in code.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAUPO_", env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://kaupo:kaupo@localhost:5432/kaupo"

    # API auth. When both are empty, auth is disabled (local dev only!).
    admin_token: str = ""
    readonly_token: str = ""

    # CORS origins allowed to call the API from a browser
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    # Strategy plugins
    strategies_dir: Path = Path("examples/strategies")

    # Exchange
    exchange: str = "kraken"

    # Alerts (ntfy topic; empty disables push alerts)
    notify_ntfy_topic: str = ""

    # Live ingestion polling
    poll_interval_seconds: float = 20.0

    # Funding-rate refresh in shadow runs (Binance USDT perp; advisory signal)
    funding_refresh_seconds: float = 1800.0

    # Default paper-trading economics
    default_quote_currency: str = "EUR"
    default_starting_cash: float = 10_000.0
    default_taker_fee_bps: float = 26.0  # Kraken ~0.26% taker
    default_maker_fee_bps: float = 16.0  # Kraken ~0.16% maker
    default_slippage_bps: float = 5.0

    @property
    def auth_disabled(self) -> bool:
        return not self.admin_token and not self.readonly_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
