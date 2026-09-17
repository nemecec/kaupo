"""Kaupo configuration.

All settings come from environment variables (prefixed ``KAUPO_``) with
sensible local-development defaults. No secrets are stored in code.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAUPO_", env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://kaupo:kaupo@localhost:5432/kaupo"

    # API auth. When all three are empty, auth is disabled (local dev only!).
    # Research: reads, backtest submission, and shadow-assignment changes.
    admin_token: str = ""
    readonly_token: str = ""
    research_token: str = ""

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

    # Live trading (Kraken spot). Credentials never appear in code, logs, or git;
    # the production host supplies them through /etc/kaupo/kaupo.env.
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    # Live runs are armed explicitly: without this, a live assignment never
    # reaches the exchange, whatever the desired-state table says.
    live_trading_enabled: bool = False
    # Ceiling on a single live order's notional, in the quote currency. Applies
    # on top of the risk-manager caps; the venue clamps an order down to it.
    live_max_notional: float = 500.0

    # Alerts (ntfy topic; empty disables push alerts)
    notify_ntfy_topic: str = ""

    # Live ingestion polling
    poll_interval_seconds: float = 20.0

    # Funding-rate refresh in shadow runs (Binance USDT perp; advisory signal)
    funding_refresh_seconds: float = 1800.0

    # Trade-tick retention: rows older than this are pruned after each ingest
    trades_retention_days: int = 30

    # Top-of-book collection (book-collector loop): poll interval and retention
    book_poll_seconds: float = 60.0
    book_retention_days: int = 30

    # Default paper-trading economics
    default_quote_currency: str = "EUR"
    default_starting_cash: float = 10_000.0
    # The live Kraken account's own tier, read from TradeVolume on 2026-09-13
    # and confirmed by the first live fill (kaupo#44). The old 26/16 pair
    # described a volume tier this account does not hold, so every backtest
    # under it understated a maker leg by 24 bps. The tier improves with
    # 30-day volume, so these stay overridable through the environment.
    default_taker_fee_bps: float = 80.0  # Kraken starter tier, 0.80% taker
    default_maker_fee_bps: float = 40.0  # Kraken starter tier, 0.40% maker
    default_slippage_bps: float = 5.0

    @model_validator(mode="after")
    def _distinct_tokens(self) -> "Settings":
        # One token value must map to one role: a shared value would make the
        # role depend on comparison order, and a leak of one would be both.
        configured = [t for t in (self.admin_token, self.readonly_token, self.research_token) if t]
        if len(configured) != len(set(configured)):
            raise ValueError("KAUPO_ADMIN_TOKEN, KAUPO_READONLY_TOKEN and KAUPO_RESEARCH_TOKEN must differ")
        return self

    @property
    def auth_disabled(self) -> bool:
        return not self.admin_token and not self.readonly_token and not self.research_token


@lru_cache
def get_settings() -> Settings:
    return Settings()


def default_taker_bps() -> float:
    """The taker fee a run models when the caller names none.

    Every request dataclass reads the tier through this one function, so the
    modelled cost and the account's real tier move together (kaupo#44).
    """
    return get_settings().default_taker_fee_bps


def default_maker_bps() -> float:
    """The maker fee a run models when the caller names none. See above."""
    return get_settings().default_maker_fee_bps
