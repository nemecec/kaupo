"""Open-interest storage: bulk upsert and range/latest queries."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import OpenInterestRow
from kaupo.domain import OpenInterest

# open-interest history is served by Binance only (Kraken futures OI is out of scope)
OI_EXCHANGE = "binance"


def _to_domain(row: OpenInterestRow) -> OpenInterest:
    return OpenInterest(
        exchange=row.exchange,
        base_asset=row.base_asset,
        ts=row.ts,
        oi_base=row.oi_base,
        oi_quote=row.oi_quote,
    )


async def upsert_open_interest(session: AsyncSession, snapshots: list[OpenInterest]) -> int:
    """Idempotent insert; existing (exchange, base_asset, ts) rows get the new values."""
    if not snapshots:
        return 0
    rows = [
        {
            "exchange": s.exchange,
            "base_asset": s.base_asset,
            "ts": s.ts,
            "oi_base": s.oi_base,
            "oi_quote": s.oi_quote,
        }
        for s in snapshots
    ]
    stmt = insert(OpenInterestRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="open_interest_pkey",
        set_={"oi_base": stmt.excluded.oi_base, "oi_quote": stmt.excluded.oi_quote},
    )
    await session.execute(stmt)
    return len(rows)


async def get_open_interest(
    session: AsyncSession,
    exchange: str,
    base_asset: str,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> list[OpenInterest]:
    """Open-interest snapshots with ts in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` points of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        OpenInterestRow.exchange == exchange,
        OpenInterestRow.base_asset == base_asset,
        OpenInterestRow.ts >= start,
        OpenInterestRow.ts < end,
    )
    if limit is not None:
        stmt = select(OpenInterestRow).where(*where).order_by(OpenInterestRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(OpenInterestRow).where(*where).order_by(OpenInterestRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_oi_range(
    session: AsyncSession, exchange: str, base_asset: str
) -> tuple[datetime | None, datetime | None, int]:
    """(first ts, last ts, count) for coverage reporting."""
    stmt = select(func.min(OpenInterestRow.ts), func.max(OpenInterestRow.ts), func.count()).where(
        OpenInterestRow.exchange == exchange,
        OpenInterestRow.base_asset == base_asset,
    )
    result = await session.execute(stmt)
    first, last, count = result.one()
    return first, last, count
