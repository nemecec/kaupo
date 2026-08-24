"""Candle storage: bulk upsert and range queries."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import CandleRow
from kaupo.domain import Candle, Pair, Timeframe


async def upsert_candles(session: AsyncSession, candles: list[Candle]) -> int:
    """Idempotent insert; existing (pair, timeframe, ts) rows are overwritten."""
    if not candles:
        return 0
    rows = [
        {
            "pair": str(c.pair),
            "timeframe": c.timeframe.value,
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    stmt = insert(CandleRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="candles_pkey",
        set_={k: getattr(stmt.excluded, k) for k in ("open", "high", "low", "close", "volume")},
    )
    await session.execute(stmt)
    return len(rows)


async def get_candles(
    session: AsyncSession,
    pair: Pair,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> list[Candle]:
    """Candles with open time in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` candles of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        CandleRow.pair == str(pair),
        CandleRow.timeframe == timeframe.value,
        CandleRow.ts >= start,
        CandleRow.ts < end,
    )
    if limit is not None:
        stmt = select(CandleRow).where(*where).order_by(CandleRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(CandleRow).where(*where).order_by(CandleRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [
        Candle(
            pair=pair,
            timeframe=timeframe,
            ts=row.ts,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in rows
    ]


async def get_latest_ts(session: AsyncSession, pair: Pair, timeframe: Timeframe) -> datetime | None:
    stmt = (
        select(CandleRow.ts)
        .where(CandleRow.pair == str(pair), CandleRow.timeframe == timeframe.value)
        .order_by(CandleRow.ts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_latest_candles(session: AsyncSession, pair: Pair, timeframe: Timeframe, n: int) -> list[Candle]:
    """The ``n`` most recent candles, ordered oldest first."""
    stmt = (
        select(CandleRow)
        .where(CandleRow.pair == str(pair), CandleRow.timeframe == timeframe.value)
        .order_by(CandleRow.ts.desc())
        .limit(n)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars())
    rows.reverse()
    return [
        Candle(
            pair=pair,
            timeframe=timeframe,
            ts=row.ts,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in rows
    ]


async def get_candle_range(
    session: AsyncSession, pair: Pair, timeframe: Timeframe
) -> tuple[datetime | None, datetime | None, int]:
    """(first ts, last ts, count) for coverage reporting."""
    from sqlalchemy import func

    stmt = select(func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).where(
        CandleRow.pair == str(pair), CandleRow.timeframe == timeframe.value
    )
    result = await session.execute(stmt)
    first, last, count = result.one()
    return first, last, count
