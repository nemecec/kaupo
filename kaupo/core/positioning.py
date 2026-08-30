"""Positioning providers: how the engines serve ``ctx.open_interest(...)``
and ``ctx.futures_metrics_daily(...)``.

Mirrors :mod:`kaupo.core.funding`: the strategy-facing context methods are
synchronous, so providers answer from memory. ``update`` runs once per
candle/step (the engines are async) and refreshes the cache; the accessors
slice it point-in-time at the virtual clock.

Both series are keyed by base asset (one dominant USDT perpetual per
venue), like funding. Open interest uses snapshot semantics (visible at
its timestamp); futures metrics are daily rows and visible only once the
UTC day is fully closed — the in-progress day never leaks.

- ``Empty*Provider``: default; no data (existing runs unchanged).
- ``Static*Provider``: a prefilled series, point-in-time filtered
  (backtests, behaviour tests).
- ``Db*Provider``: reads the newest rows from Postgres per update
  (shadow/live runs; one cheap indexed query per candle).
"""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.futures_metrics import METRICS_EXCHANGE, get_latest_futures_metrics_daily
from kaupo.data.open_interest import OI_EXCHANGE, get_latest_open_interest
from kaupo.db.session import sm_scope
from kaupo.domain import FuturesMetricsDaily, OpenInterest


class OpenInterestProvider(Protocol):
    async def update(self, base_asset: str, now: datetime) -> None:
        """Refresh the cached series for ``base_asset`` up to ``now``."""
        ...

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[OpenInterest]:
        """Up to ``n`` snapshots at or before ``now``, oldest first."""
        ...


class EmptyOpenInterestProvider:
    """No open-interest data: ``latest`` is always empty."""

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[OpenInterest]:
        return []


class StaticOpenInterestProvider:
    """Serves open_interest() from a prefilled series, point-in-time filtered."""

    def __init__(self, by_base_asset: Mapping[str, Sequence[OpenInterest]]) -> None:
        self._points: dict[str, tuple[OpenInterest, ...]] = {}
        self._timestamps: dict[str, list[datetime]] = {}
        for base, rows in by_base_asset.items():
            ordered = tuple(sorted(rows, key=lambda r: r.ts))
            key = base.upper()
            self._points[key] = ordered
            self._timestamps[key] = [r.ts for r in ordered]

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[OpenInterest]:
        points = self._points.get(base_asset.upper())
        if not points or n <= 0:
            return []
        idx = bisect_right(self._timestamps[base_asset.upper()], now)
        return list(points[max(0, idx - n) : idx])


class DbOpenInterestProvider:
    """Serves open_interest() in shadow/live runs from Postgres.

    ``update`` loads the ``cap`` newest snapshots at or before ``now``;
    ``latest`` slices that cache, so strategy calls stay synchronous and
    point-in-time.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        exchange: str = OI_EXCHANGE,
        cap: int = 300,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._exchange = exchange
        self._cap = cap
        self._cache: dict[str, list[OpenInterest]] = {}

    async def update(self, base_asset: str, now: datetime) -> None:
        async with sm_scope(self._sessionmaker) as session:
            rows = await get_latest_open_interest(session, self._exchange, base_asset.upper(), self._cap, now)
        self._cache[base_asset.upper()] = rows

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[OpenInterest]:
        if n <= 0:
            return []
        points = self._cache.get(base_asset.upper(), [])
        return points[-n:] if n < len(points) else points


class FuturesMetricsProvider(Protocol):
    async def update(self, base_asset: str, now: datetime) -> None:
        """Refresh the cached series for ``base_asset`` up to ``now``."""
        ...

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FuturesMetricsDaily]:
        """Up to ``n`` daily rows with the aggregated day closed at ``now``, oldest first."""
        ...


class EmptyFuturesMetricsProvider:
    """No futures-metrics data: ``latest`` is always empty."""

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FuturesMetricsDaily]:
        return []


class StaticFuturesMetricsProvider:
    """Serves futures_metrics_daily() from a prefilled series; only fully closed days."""

    def __init__(self, by_base_asset: Mapping[str, Sequence[FuturesMetricsDaily]]) -> None:
        self._rows: dict[str, tuple[FuturesMetricsDaily, ...]] = {}
        for base, rows in by_base_asset.items():
            ordered = tuple(sorted(rows, key=lambda r: r.day))
            self._rows[base.upper()] = ordered

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FuturesMetricsDaily]:
        rows = self._rows.get(base_asset.upper())
        if not rows or n <= 0:
            return []
        closed = [r for r in rows if r.day < now.date()]  # the in-progress day never leaks
        return list(closed[-n:])


class DbFuturesMetricsProvider:
    """Serves futures_metrics_daily() in shadow/live runs from Postgres.

    ``update`` loads the ``cap`` newest fully closed day rows at ``now``;
    ``latest`` slices that cache, so strategy calls stay synchronous and
    point-in-time.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        exchange: str = METRICS_EXCHANGE,
        cap: int = 400,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._exchange = exchange
        self._cap = cap
        self._cache: dict[str, list[FuturesMetricsDaily]] = {}

    async def update(self, base_asset: str, now: datetime) -> None:
        async with sm_scope(self._sessionmaker) as session:
            rows = await get_latest_futures_metrics_daily(
                session, self._exchange, base_asset.upper(), self._cap, now.date()
            )
        self._cache[base_asset.upper()] = rows

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FuturesMetricsDaily]:
        if n <= 0:
            return []
        rows = self._cache.get(base_asset.upper(), [])
        return rows[-n:] if n < len(rows) else rows
