"""Funding-rate providers: how the engines serve ``ctx.funding(...)``.

The strategy-facing context method is synchronous, so providers answer from
memory. ``update`` runs once per candle/step (the engines are async) and
refreshes the cache; ``latest`` slices it point-in-time at the virtual clock.

- ``EmptyFundingProvider``: default; no funding data (existing runs unchanged).
- ``StaticFundingProvider``: a prefilled series, point-in-time filtered
  (backtests, behaviour tests).
- ``DbFundingProvider``: reads the latest points from Postgres per update
  (shadow/live runs; funding has ~3 points/day, so the query is cheap).
"""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.funding import FUNDING_EXCHANGE, get_latest_funding_rates
from kaupo.db.session import sm_scope
from kaupo.domain import FundingRate


class FundingProvider(Protocol):
    async def update(self, base_asset: str, now: datetime) -> None:
        """Refresh the cached series for ``base_asset`` up to ``now``."""
        ...

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FundingRate]:
        """Up to ``n`` funding points at or before ``now``, oldest first."""
        ...


class EmptyFundingProvider:
    """No funding data: ``latest`` is always empty."""

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FundingRate]:
        return []


class StaticFundingProvider:
    """Serves funding() from a prefilled series, point-in-time filtered."""

    def __init__(self, by_base_asset: Mapping[str, Sequence[FundingRate]]) -> None:
        self._points: dict[str, tuple[FundingRate, ...]] = {}
        self._timestamps: dict[str, list[datetime]] = {}
        for base, rates in by_base_asset.items():
            ordered = tuple(sorted(rates, key=lambda r: r.ts))
            key = base.upper()
            self._points[key] = ordered
            self._timestamps[key] = [r.ts for r in ordered]

    async def update(self, base_asset: str, now: datetime) -> None:
        return None

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FundingRate]:
        points = self._points.get(base_asset.upper())
        if not points or n <= 0:
            return []
        idx = bisect_right(self._timestamps[base_asset.upper()], now)
        return list(points[max(0, idx - n) : idx])


class DbFundingProvider:
    """Serves funding() in shadow/live runs from Postgres.

    ``update`` loads the ``cap`` newest points at or before ``now`` into
    memory (one cheap query per candle/step); ``latest`` slices that cache,
    so strategy calls stay synchronous and point-in-time.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        exchange: str = FUNDING_EXCHANGE,
        cap: int = 300,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._exchange = exchange
        self._cap = cap
        self._cache: dict[str, list[FundingRate]] = {}

    async def update(self, base_asset: str, now: datetime) -> None:
        async with sm_scope(self._sessionmaker) as session:
            rows = await get_latest_funding_rates(
                session, self._exchange, base_asset.upper(), self._cap, before=now
            )
        self._cache[base_asset.upper()] = rows

    def latest(self, base_asset: str, n: int, now: datetime) -> Sequence[FundingRate]:
        if n <= 0:
            return []
        points = self._cache.get(base_asset.upper(), [])
        return points[-n:] if n < len(points) else points
