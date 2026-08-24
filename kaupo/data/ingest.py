"""Historical backfill and live candle polling."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.candles import upsert_candles
from kaupo.data.kraken import KrakenClient
from kaupo.domain import Candle, Pair, Timeframe

log = logging.getLogger(__name__)


async def backfill(
    client: KrakenClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    timeframe: Timeframe,
    start: datetime,
    end: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Page through Kraken history from ``start`` and upsert. Returns candle count."""
    end = end or datetime.now(UTC)
    step = timedelta(seconds=timeframe.seconds)
    since = start
    prev_last: datetime | None = None
    total = 0

    while since < end:
        batch = await client.fetch_candles(pair, timeframe, since=since)
        batch = [c for c in batch if c.ts < end]
        if not batch:
            break
        last_ts = batch[-1].ts
        if prev_last is not None and last_ts <= prev_last:
            log.warning("Backfill made no progress at %s; stopping", since)
            break
        async with sessionmaker() as session:
            await upsert_candles(session, batch)
            await session.commit()
        total += len(batch)
        if on_progress:
            on_progress(total)

        prev_last = last_ts
        since = last_ts + step

    return total


class LiveCandlePoller:
    """Polls the exchange for the newest candles and yields newly *closed* ones.

    First poll establishes a baseline and returns at most the latest closed
    candle; subsequent polls return everything closed since the baseline.
    """

    def __init__(
        self,
        client: KrakenClient,
        pair: Pair,
        timeframe: Timeframe,
        poll_interval_seconds: float = 20.0,
        baseline: datetime | None = None,
    ) -> None:
        self._client = client
        self._pair = pair
        self._timeframe = timeframe
        self._poll_interval = poll_interval_seconds
        # seeding the baseline (e.g. from the warm-up tail) makes the gap
        # refill path pick up anything closed between warm-up and first poll
        self._baseline: datetime | None = baseline

    async def poll_once(self) -> list[Candle]:
        window = timedelta(seconds=4 * self._timeframe.seconds)
        since = datetime.now(UTC) - window
        if self._baseline is not None and self._baseline < since:
            # outage longer than the fetch window: backfill the gap instead
            # of silently skipping candles (parity + store integrity)
            log.warning("Poll gap detected after %s; backfilling", self._baseline)
            since = self._baseline + timedelta(seconds=self._timeframe.seconds)
        candles = await self._client.fetch_candles(self._pair, self._timeframe, since=since)
        if not candles:
            return []
        if self._baseline is None:
            self._baseline = candles[-1].ts
            return [candles[-1]]
        new = [c for c in candles if c.ts > self._baseline]
        if new:
            self._baseline = new[-1].ts
        return new

    async def stream(self, stop: asyncio.Event | None = None) -> AsyncIterator[Candle]:
        """Endlessly yield newly closed candles until ``stop`` is set."""
        while stop is None or not stop.is_set():
            try:
                for candle in await self.poll_once():
                    yield candle
            except Exception:
                log.exception("Polling failed; retrying in %ss", self._poll_interval)
            await asyncio.sleep(self._poll_interval)
