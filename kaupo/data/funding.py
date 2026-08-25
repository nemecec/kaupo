"""Funding-rate storage: bulk upsert and range/latest queries."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import FundingRateRow
from kaupo.domain import FundingRate

# funding history is served by Binance only (Kraken funding is out of scope)
FUNDING_EXCHANGE = "binance"


def _to_domain(row: FundingRateRow) -> FundingRate:
    return FundingRate(exchange=row.exchange, base_asset=row.base_asset, ts=row.ts, rate=row.rate)


async def upsert_funding_rates(session: AsyncSession, rates: list[FundingRate]) -> int:
    """Idempotent insert; existing (exchange, base_asset, ts) rows get the new rate."""
    if not rates:
        return 0
    rows = [
        {
            "exchange": r.exchange,
            "base_asset": r.base_asset,
            "ts": r.ts,
            "rate": r.rate,
        }
        for r in rates
    ]
    stmt = insert(FundingRateRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="funding_rates_pkey",
        set_={"rate": stmt.excluded.rate},
    )
    await session.execute(stmt)
    return len(rows)


async def get_funding_rates(
    session: AsyncSession,
    exchange: str,
    base_asset: str,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> list[FundingRate]:
    """Funding rates with funding time in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` points of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        FundingRateRow.exchange == exchange,
        FundingRateRow.base_asset == base_asset,
        FundingRateRow.ts >= start,
        FundingRateRow.ts < end,
    )
    if limit is not None:
        stmt = select(FundingRateRow).where(*where).order_by(FundingRateRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(FundingRateRow).where(*where).order_by(FundingRateRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_latest_funding_rates(
    session: AsyncSession, exchange: str, base_asset: str, n: int, before: datetime
) -> list[FundingRate]:
    """The ``n`` most recent funding rates at or before ``before``, oldest first."""
    stmt = (
        select(FundingRateRow)
        .where(
            FundingRateRow.exchange == exchange,
            FundingRateRow.base_asset == base_asset,
            FundingRateRow.ts <= before,
        )
        .order_by(FundingRateRow.ts.desc())
        .limit(n)
    )
    rows = list((await session.execute(stmt)).scalars())
    rows.reverse()
    return [_to_domain(row) for row in rows]


async def get_latest_funding_ts(session: AsyncSession, exchange: str, base_asset: str) -> datetime | None:
    stmt = (
        select(FundingRateRow.ts)
        .where(
            FundingRateRow.exchange == exchange,
            FundingRateRow.base_asset == base_asset,
        )
        .order_by(FundingRateRow.ts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_funding_range(
    session: AsyncSession, exchange: str, base_asset: str
) -> tuple[datetime | None, datetime | None, int]:
    """(first ts, last ts, count) for coverage reporting."""
    stmt = select(func.min(FundingRateRow.ts), func.max(FundingRateRow.ts), func.count()).where(
        FundingRateRow.exchange == exchange,
        FundingRateRow.base_asset == base_asset,
    )
    result = await session.execute(stmt)
    first, last, count = result.one()
    return first, last, count
