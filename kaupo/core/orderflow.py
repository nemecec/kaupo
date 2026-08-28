"""Order-flow providers: how the engines serve ``ctx.ticks/book/tick_flow``.

Mirrors :mod:`kaupo.core.funding`: the strategy-facing context methods are
synchronous, so providers answer from memory. ``update`` runs once per
candle/step (the engines are async) and refreshes the cache; the accessors
slice it point-in-time at the virtual clock.

- ``EmptyOrderFlowProvider``: default; no order-flow data (existing runs
  unchanged).
- ``StaticOrderFlowProvider``: prefilled series, point-in-time filtered
  (tests).
- ``DbOrderFlowProvider``: reads ticks and book snapshots from Postgres
  (backtests and shadow/live runs; the volume makes preloading a whole
  window unreasonable, so it queries per candle instead). The permanent
  daily aggregates behind ``tick_flow_daily`` are one row per pair and day,
  so they reload in full per update, like the funding provider.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.book import get_recent_book_snapshots
from kaupo.data.orderflow_daily import get_orderflow_daily
from kaupo.data.trades import get_recent_trade_ticks
from kaupo.db.session import sm_scope
from kaupo.domain import BookSnapshot, OrderflowDaily, TickFlow, TradeTick

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class OrderFlowProvider(Protocol):
    async def update(self, pair: str, now: datetime) -> None:
        """Refresh the cached series for ``pair`` (unified string) up to ``now``."""
        ...

    def ticks(self, pair: str, n: int, now: datetime) -> Sequence[TradeTick]:
        """Up to ``n`` trade ticks at or before ``now``, oldest first."""
        ...

    def book(self, pair: str, n: int, now: datetime) -> Sequence[BookSnapshot]:
        """Up to ``n`` book snapshots at or before ``now``, oldest first."""
        ...

    def tick_flow(self, pair: str, n: int, now: datetime, candle_seconds: int) -> Sequence[TickFlow]:
        """Per-candle order-flow buckets over the last ``n`` completed candles."""
        ...

    def tick_flow_daily(self, pair: str, n: int, now: datetime) -> Sequence[OrderflowDaily]:
        """Up to ``n`` daily aggregates with the aggregated day closed at ``now``, oldest first."""
        ...


@dataclass
class _FlowAcc:
    buy_count: int = 0
    sell_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    max_trade_size: float = 0.0


def bucket_tick_flow(
    ticks: Sequence[TradeTick], n: int, now: datetime, candle_seconds: int
) -> list[TickFlow]:
    """Aggregate ticks into per-candle :class:`TickFlow` buckets, oldest first.

    A tick's bucket is the ``candle_seconds``-aligned window holding its
    trade time (floor-epoch grouping). Only COMPLETED buckets — bucket end at
    or before ``now`` — are returned, so the in-progress candle never leaks.
    Candles without trades are absent; the series is empty when no tick falls
    in a completed bucket. At most the newest ``n`` buckets.
    """
    if n <= 0:
        return []
    step = timedelta(seconds=candle_seconds)
    buckets: dict[datetime, _FlowAcc] = {}
    for tick in ticks:
        start = _EPOCH + ((tick.ts - _EPOCH) // step) * step
        if start + step > now:
            continue  # bucket still open at the clock: not visible yet
        acc = buckets.setdefault(start, _FlowAcc())
        if tick.side == "buy":
            acc.buy_count += 1
            acc.buy_volume += tick.size
        else:
            acc.sell_count += 1
            acc.sell_volume += tick.size
        acc.max_trade_size = max(acc.max_trade_size, tick.size)
    return [
        TickFlow(
            ts=start,
            buy_count=buckets[start].buy_count,
            sell_count=buckets[start].sell_count,
            buy_volume=buckets[start].buy_volume,
            sell_volume=buckets[start].sell_volume,
            max_trade_size=buckets[start].max_trade_size,
        )
        for start in sorted(buckets)[-n:]
    ]


class EmptyOrderFlowProvider:
    """No order-flow data: every accessor is always empty."""

    async def update(self, pair: str, now: datetime) -> None:
        return None

    def ticks(self, pair: str, n: int, now: datetime) -> Sequence[TradeTick]:
        return []

    def book(self, pair: str, n: int, now: datetime) -> Sequence[BookSnapshot]:
        return []

    def tick_flow(self, pair: str, n: int, now: datetime, candle_seconds: int) -> Sequence[TickFlow]:
        return []

    def tick_flow_daily(self, pair: str, n: int, now: datetime) -> Sequence[OrderflowDaily]:
        return []


class StaticOrderFlowProvider:
    """Serves ticks/book/tick_flow/tick_flow_daily from prefilled series, point-in-time filtered."""

    def __init__(
        self,
        ticks: Mapping[str, Sequence[TradeTick]] | None = None,
        book: Mapping[str, Sequence[BookSnapshot]] | None = None,
        daily: Mapping[str, Sequence[OrderflowDaily]] | None = None,
    ) -> None:
        self._ticks: dict[str, tuple[TradeTick, ...]] = {}
        self._tick_ts: dict[str, list[datetime]] = {}
        for pair, series in (ticks or {}).items():
            ordered = tuple(sorted(series, key=lambda t: t.ts))
            self._ticks[pair] = ordered
            self._tick_ts[pair] = [t.ts for t in ordered]
        self._book: dict[str, tuple[BookSnapshot, ...]] = {}
        self._book_ts: dict[str, list[datetime]] = {}
        for book_pair, snapshots in (book or {}).items():
            ordered_snapshots = tuple(sorted(snapshots, key=lambda s: s.ts))
            self._book[book_pair] = ordered_snapshots
            self._book_ts[book_pair] = [s.ts for s in ordered_snapshots]
        self._daily: dict[str, tuple[OrderflowDaily, ...]] = {}
        self._daily_days: dict[str, list[date]] = {}
        for daily_pair, rows in (daily or {}).items():
            ordered_rows = tuple(sorted(rows, key=lambda r: r.day))
            self._daily[daily_pair] = ordered_rows
            self._daily_days[daily_pair] = [r.day for r in ordered_rows]

    async def update(self, pair: str, now: datetime) -> None:
        return None

    def ticks(self, pair: str, n: int, now: datetime) -> Sequence[TradeTick]:
        series = self._ticks.get(pair)
        if not series or n <= 0:
            return []
        idx = bisect_right(self._tick_ts[pair], now)
        return list(series[max(0, idx - n) : idx])

    def book(self, pair: str, n: int, now: datetime) -> Sequence[BookSnapshot]:
        series = self._book.get(pair)
        if not series or n <= 0:
            return []
        idx = bisect_right(self._book_ts[pair], now)
        return list(series[max(0, idx - n) : idx])

    def tick_flow(self, pair: str, n: int, now: datetime, candle_seconds: int) -> Sequence[TickFlow]:
        series = self._ticks.get(pair)
        if not series or n <= 0:
            return []
        idx = bisect_right(self._tick_ts[pair], now)
        return bucket_tick_flow(series[:idx], n, now, candle_seconds)

    def tick_flow_daily(self, pair: str, n: int, now: datetime) -> Sequence[OrderflowDaily]:
        series = self._daily.get(pair)
        if not series or n <= 0:
            return []
        # a day's row is visible once that UTC day is fully closed at the clock
        idx = bisect_left(self._daily_days[pair], now.date())
        return list(series[max(0, idx - n) : idx])


class DbOrderFlowProvider:
    """Serves ticks/book/tick_flow from Postgres (backtests, shadow/live).

    Tick and book volume rules out preloading a whole backtest window into
    memory, so ``update`` keeps an incremental cache per pair instead: the
    first update loads the newest ``cap`` rows at or before ``now``; later
    updates re-read only the recent ``refresh_window`` and merge it over the
    cache tail (rows land slightly late in live collection, so the tail must
    stay re-readable). One bounded, indexed query per series per candle.

    The daily aggregates behind ``tick_flow_daily`` are one row per pair and
    day, so ``update`` simply reloads the newest ``daily_cap`` rows whose day
    is fully closed at ``now`` (like the funding provider) — no incremental
    merge needed at that volume.

    The sync accessors slice that cache, so strategy calls stay point-in-time
    and cheap. Coverage is count-bounded by ``cap``: reads asking for more
    history than the cache holds simply return fewer rows/buckets.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        exchange: str = "kraken",
        cap: int = 50_000,
        refresh_window: timedelta = timedelta(hours=1),
        daily_cap: int = 1500,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._exchange = exchange
        self._cap = cap
        self._refresh_window = refresh_window
        self._daily_cap = daily_cap
        self._ticks: dict[str, list[TradeTick]] = {}
        self._book: dict[str, list[BookSnapshot]] = {}
        self._daily: dict[str, list[OrderflowDaily]] = {}

    async def update(self, pair: str, now: datetime) -> None:
        async with sm_scope(self._sessionmaker) as session:
            self._ticks[pair] = await self._refresh_ticks(pair, now, session)
            self._book[pair] = await self._refresh_book(pair, now, session)
            # [epoch, today): only days fully closed at the clock are served
            self._daily[pair] = await get_orderflow_daily(
                session, self._exchange, pair, _EPOCH.date(), now.date(), limit=self._daily_cap
            )

    async def _refresh_ticks(self, pair: str, now: datetime, session: AsyncSession) -> list[TradeTick]:
        cached = self._ticks.get(pair, [])
        if not cached or cached[-1].ts > now:
            # first sight of the pair (or a rewound clock, which engines never
            # do): load the newest cap rows at or before now
            return await get_recent_trade_ticks(session, self._exchange, pair, now, limit=self._cap)
        start = cached[-1].ts - self._refresh_window
        rows = await get_recent_trade_ticks(session, self._exchange, pair, now, start=start)
        cut = len(cached)
        while cut > 0 and cached[cut - 1].ts >= start:
            cut -= 1
        return (cached[:cut] + rows)[-self._cap :]

    async def _refresh_book(self, pair: str, now: datetime, session: AsyncSession) -> list[BookSnapshot]:
        cached = self._book.get(pair, [])
        if not cached or cached[-1].ts > now:
            return await get_recent_book_snapshots(session, self._exchange, pair, now, limit=self._cap)
        start = cached[-1].ts - self._refresh_window
        rows = await get_recent_book_snapshots(session, self._exchange, pair, now, start=start)
        cut = len(cached)
        while cut > 0 and cached[cut - 1].ts >= start:
            cut -= 1
        return (cached[:cut] + rows)[-self._cap :]

    def ticks(self, pair: str, n: int, now: datetime) -> Sequence[TradeTick]:
        if n <= 0:
            return []
        rows = self._ticks.get(pair, [])
        return rows[-n:] if n < len(rows) else rows

    def book(self, pair: str, n: int, now: datetime) -> Sequence[BookSnapshot]:
        if n <= 0:
            return []
        rows = self._book.get(pair, [])
        return rows[-n:] if n < len(rows) else rows

    def tick_flow(self, pair: str, n: int, now: datetime, candle_seconds: int) -> Sequence[TickFlow]:
        return bucket_tick_flow(self._ticks.get(pair, []), n, now, candle_seconds)

    def tick_flow_daily(self, pair: str, n: int, now: datetime) -> Sequence[OrderflowDaily]:
        if n <= 0:
            return []
        rows = self._daily.get(pair, [])
        return rows[-n:] if n < len(rows) else rows
