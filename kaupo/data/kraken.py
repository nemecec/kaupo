"""Kraken market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import ccxt.async_support as ccxt

from kaupo.domain import Candle, Pair, Timeframe

log = logging.getLogger(__name__)

KRAKEN_PAGE_SIZE = 720  # Kraken returns at most 720 OHLC entries per call
# margin for clock skew / exchange-side finalization before a candle counts as closed
CLOSE_GRACE = timedelta(seconds=2)


class KrakenClient:
    """Thin async wrapper around ccxt's Kraken exchange, returning domain candles.

    Only *complete* candles are ever returned (the in-progress candle is dropped).
    """

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._exchange = ccxt.kraken({"enableRateLimit": True})
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _valid(o: float, h: float, low: float, c: float, v: float) -> bool:
        values = (o, h, low, c, v)
        return (
            all(math.isfinite(x) for x in values)
            and o > 0
            and h > 0
            and low > 0
            and c > 0
            and h >= low
            and low <= o <= h
            and low <= c <= h
            and v >= 0
        )

    async def close(self) -> None:
        await self._exchange.close()

    async def __aenter__(self) -> KrakenClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def fetch_candles(
        self,
        pair: Pair,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int = KRAKEN_PAGE_SIZE,
    ) -> list[Candle]:
        since_ms = int(since.timestamp() * 1000) if since else None
        ohlcv = await self._exchange.fetch_ohlcv(str(pair), timeframe.value, since=since_ms, limit=limit)
        now = self._now()
        candles = []
        for ts_ms, o, h, low, c, v in ohlcv:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            if ts + timedelta(seconds=timeframe.seconds) + CLOSE_GRACE > now:
                continue  # in-progress candle
            if not self._valid(o, h, low, c, v):
                log.warning(
                    "Dropping invalid candle %s %s %s: %s", pair, timeframe.value, ts, (o, h, low, c, v)
                )
                continue
            candles.append(
                Candle(
                    pair=pair,
                    timeframe=timeframe,
                    ts=ts,
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=float(v),
                )
            )
        return candles
