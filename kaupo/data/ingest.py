"""Historical backfill and live candle polling."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.binance import BinanceClient
from kaupo.data.candles import upsert_candles
from kaupo.data.ccxt_client import CcxtExchangeClient
from kaupo.data.funding import upsert_funding_rates
from kaupo.data.kraken import KrakenClient
from kaupo.data.open_interest import upsert_open_interest
from kaupo.data.trades import upsert_trade_ticks
from kaupo.domain import Candle, Pair, Timeframe

log = logging.getLogger(__name__)


async def backfill(
    client: CcxtExchangeClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    timeframe: Timeframe,
    start: datetime,
    end: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Page through exchange history from ``start`` and upsert. Returns candle count."""
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


async def backfill_open_interest(
    client: BinanceClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    base_asset: str,
    start: datetime,
    end: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Page through open-interest history from ``start`` and upsert. Returns row count.

    Snapshots sit on a fixed hourly grid, but Binance only serves ~30 days
    back, so paging advances a millisecond past the last seen snapshot
    instead of a fixed step (same idea as funding).
    """
    end = end or datetime.now(UTC)
    step = timedelta(milliseconds=1)
    since = start
    prev_last: datetime | None = None
    total = 0

    while since < end:
        batch = await client.fetch_open_interest_history(base_asset, since=since)
        batch = [s for s in batch if s.ts < end]
        if not batch:
            break
        last_ts = batch[-1].ts
        if prev_last is not None and last_ts <= prev_last:
            log.warning("Open-interest backfill made no progress at %s; stopping", since)
            break
        async with sessionmaker() as session:
            await upsert_open_interest(session, batch)
            await session.commit()
        total += len(batch)
        if on_progress:
            on_progress(total)

        prev_last = last_ts
        since = last_ts + step

    return total


async def backfill_funding(
    client: BinanceClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    base_asset: str,
    start: datetime,
    end: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Page through funding history from ``start`` and upsert. Returns rate count.

    Funding times are discrete events (no fixed grid), so paging advances a
    millisecond past the last seen funding time instead of a fixed step.
    """
    end = end or datetime.now(UTC)
    step = timedelta(milliseconds=1)
    since = start
    prev_last: datetime | None = None
    total = 0

    while since < end:
        batch = await client.fetch_funding_rates(base_asset, since=since)
        batch = [r for r in batch if r.ts < end]
        if not batch:
            break
        last_ts = batch[-1].ts
        if prev_last is not None and last_ts <= prev_last:
            log.warning("Funding backfill made no progress at %s; stopping", since)
            break
        async with sessionmaker() as session:
            await upsert_funding_rates(session, batch)
            await session.commit()
        total += len(batch)
        if on_progress:
            on_progress(total)

        prev_last = last_ts
        since = last_ts + step

    return total


async def backfill_trades(
    client: CcxtExchangeClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    start: datetime,
    end: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Page through public trades from ``start`` and upsert. Returns tick count.

    Paging mirrors the other backfills, with two venue differences proven by
    the pair-quality spike (scripts/pair_quality.py). Kraken pages by its
    nanosecond response cursor (ccxt's ``since`` is broken there); the client
    exposes it as ``trades_cursor`` and gets it back on the next call. The
    cursor is inclusive — the previous page's tail trades are re-served on
    the next one — so re-served duplicates are skipped, and a page with
    nothing new means the live edge. Binance serves aggTrades in one-hour
    windows, so an empty page can mean an empty hour rather than the live
    edge; the window is skipped forward instead of stopping.
    """
    end = end or datetime.now(UTC)
    step = timedelta(milliseconds=1)
    since = start
    cursor: str | None = None
    prev_last: datetime | None = None
    prev_tail: set[tuple[datetime, float, float, str]] = set()
    total = 0

    while since < end:
        sent_cursor = cursor
        page = await client.fetch_trades(pair, since=since, cursor=cursor)
        cursor = client.trades_cursor
        batch = [t for t in page if t.ts < end]
        if not batch:
            window = client.trades_page_window
            if not page and window is not None and since + window < end:
                since += window  # empty window (e.g. a quiet hour), not the live edge
                continue
            break
        new = [t for t in batch if (t.ts, t.price, t.size, t.side) not in prev_tail]
        if not new:
            break  # only re-served duplicates: caught up to the live edge
        last_ts = new[-1].ts
        cursor_advanced = sent_cursor is not None and cursor is not None and cursor != sent_cursor
        if prev_last is not None and last_ts <= prev_last and not cursor_advanced:
            log.warning("Trade backfill made no progress at %s; stopping", since)
            break
        async with sessionmaker() as session:
            await upsert_trade_ticks(session, new)
            await session.commit()
        total += len(new)
        if on_progress:
            on_progress(total)

        prev_last = last_ts
        prev_tail = {(t.ts, t.price, t.size, t.side) for t in new if t.ts == last_ts}
        since = last_ts + step

    return total


class LiveCandlePoller:
    """Polls the exchange for the newest candles and yields newly *closed* ones.

    First poll establishes a baseline and returns at most the latest closed
    candle; subsequent polls return everything closed since the baseline.
    """

    # a poll is owed a candle once the baseline's successor should have
    # closed: the baseline candle closes one timeframe after its ts, the
    # successor one timeframe after that, plus slack
    OWED_SLACK = timedelta(minutes=2)

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
        # seeding the baseline (e.g. from the warm-up tail) makes the first
        # poll pick up anything closed between warm-up and start
        self._baseline: datetime | None = baseline
        self._empty_streak = 0

    async def poll_once(self) -> list[Candle]:
        if self._baseline is not None:
            # anchored to delivered progress: a sliding now-window slides past
            # a just-closed candle at exact boundaries (a 4h candle is exactly
            # one window old at its own close) and can never fetch it again.
            # A long outage pages forward 720 candles per poll instead.
            since = self._baseline + timedelta(seconds=1)
        else:
            since = datetime.now(UTC) - timedelta(seconds=4 * self._timeframe.seconds)
        candles = await self._client.fetch_candles(self._pair, self._timeframe, since=since)
        if self._baseline is None:
            if not candles:
                return []
            # cold start: the whole fetched window is new
            self._baseline = candles[-1].ts
            return candles
        new = [c for c in candles if c.ts > self._baseline]
        if new:
            self._empty_streak = 0
            self._baseline = new[-1].ts
        else:
            self._log_if_owed(candles)
        return new

    def _log_if_owed(self, candles: list[Candle]) -> None:
        """Warn on owed-but-empty polls — the 2026-08-31 silent-stall shape.

        A candle should have closed since the baseline, yet the fetch delivered
        nothing new. The response contents discriminate the causes: empty (the
        venue or this client instance serves nothing) vs stale (newest row at
        or behind the baseline — venue-side commitment lag).
        """
        assert self._baseline is not None
        deadline = self._baseline + timedelta(seconds=2 * self._timeframe.seconds) + self.OWED_SLACK
        if datetime.now(UTC) < deadline:
            self._empty_streak = 0
            return
        self._empty_streak += 1
        if self._empty_streak == 1 or self._empty_streak % 10 == 0:
            log.warning(
                "Poll owed a candle since %s (%s %s) but fetch returned %d rows (newest %s); streak %d",
                self._baseline,
                self._pair,
                self._timeframe.value,
                len(candles),
                candles[-1].ts if candles else None,
                self._empty_streak,
            )

    async def stream(self, stop: asyncio.Event | None = None) -> AsyncIterator[Candle]:
        """Endlessly yield newly closed candles until ``stop`` is set."""
        while stop is None or not stop.is_set():
            try:
                for candle in await self.poll_once():
                    yield candle
            except Exception:
                log.exception("Polling failed; retrying in %ss", self._poll_interval)
            await asyncio.sleep(self._poll_interval)
