"""Binance market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime

import ccxt.async_support as ccxt

from kaupo.data.ccxt_client import CcxtExchangeClient
from kaupo.domain import FundingRate

log = logging.getLogger(__name__)

BINANCE_PAGE_SIZE = 1500  # Binance returns at most 1500 klines per call
BINANCE_FUNDING_PAGE_SIZE = 1000  # Binance returns at most 1000 funding entries per call


class BinanceClient(CcxtExchangeClient):
    """Binance candles; serves deep history (paginates forward from ``since``).

    Also serves perpetual-futures funding history from the separate
    ``binanceusdm`` market (one dominant USDT perpetual per base asset).
    """

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__("binance", BINANCE_PAGE_SIZE, now)
        self._futures = ccxt.binanceusdm({"enableRateLimit": True})

    async def close(self) -> None:
        await super().close()
        await self._futures.close()

    async def fetch_funding_rates(
        self,
        base_asset: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingRate]:
        """Funding history of the base asset's USDT-margined perpetual, ascending.

        Unknown perps surface as ccxt ``BadSymbol``; malformed rows are dropped.
        """
        if limit is None:
            limit = BINANCE_FUNDING_PAGE_SIZE
        base = base_asset.strip().upper()
        if not base:
            raise ValueError(f"Invalid base asset {base_asset!r}")
        since_ms = int(since.timestamp() * 1000) if since else None
        history = await self._futures.fetch_funding_rate_history(
            f"{base}/USDT:USDT", since=since_ms, limit=limit
        )
        rates = []
        for entry in history:
            ts_ms = entry.get("timestamp")
            rate = entry.get("fundingRate")
            if not ts_ms or ts_ms <= 0 or rate is None or not math.isfinite(rate):
                log.warning("Dropping malformed funding row %s: %s", base, entry)
                continue
            rates.append(
                FundingRate(
                    exchange="binance",
                    base_asset=base,
                    ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                    rate=float(rate),
                )
            )
        return rates
