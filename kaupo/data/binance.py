"""Binance market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from kaupo.data.ccxt_client import CcxtExchangeClient

BINANCE_PAGE_SIZE = 1500  # Binance returns at most 1500 klines per call


class BinanceClient(CcxtExchangeClient):
    """Binance candles; serves deep history (paginates forward from ``since``)."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__("binance", BINANCE_PAGE_SIZE, now)
