"""Kraken market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from kaupo.data.ccxt_client import CcxtExchangeClient

KRAKEN_PAGE_SIZE = 720  # Kraken returns at most 720 OHLC entries per call


class KrakenClient(CcxtExchangeClient):
    """Kraken candles; only the newest ``KRAKEN_PAGE_SIZE`` per timeframe are served."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__("kraken", KRAKEN_PAGE_SIZE, now)
