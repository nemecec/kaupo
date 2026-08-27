"""Trade-tick storage: bulk upsert, range/latest queries, retention pruning."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import TradeTickRow
from kaupo.domain import TradeTick


def _to_domain(row: TradeTickRow) -> TradeTick:
    return TradeTick(
        exchange=row.exchange,
        pair=row.pair,
        ts=row.ts,
        price=row.price,
        size=row.size,
        side=row.side,
    )


async def upsert_trade_ticks(session: AsyncSession, ticks: list[TradeTick]) -> int:
    """Insert ticks; rows already in the table (same full key) are skipped.

    Returns the number of ticks handed to the insert; ON CONFLICT DO NOTHING
    drops duplicates (including same-ms identical ticks) silently, so the
    inserted count can be lower.
    """
    if not ticks:
        return 0
    rows = [
        {
            "exchange": t.exchange,
            "pair": t.pair,
            "ts": t.ts,
            "price": t.price,
            "size": t.size,
            "side": t.side,
        }
        for t in ticks
    ]
    stmt = insert(TradeTickRow).values(rows).on_conflict_do_nothing()
    await session.execute(stmt)
    return len(rows)


async def get_trade_ticks(
    session: AsyncSession,
    exchange: str,
    pair: str,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> list[TradeTick]:
    """Trade ticks with trade time in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` ticks of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        TradeTickRow.exchange == exchange,
        TradeTickRow.pair == pair,
        TradeTickRow.ts >= start,
        TradeTickRow.ts < end,
    )
    if limit is not None:
        stmt = select(TradeTickRow).where(*where).order_by(TradeTickRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(TradeTickRow).where(*where).order_by(TradeTickRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_recent_trade_ticks(
    session: AsyncSession,
    exchange: str,
    pair: str,
    before: datetime,
    start: datetime | None = None,
    limit: int | None = None,
) -> list[TradeTick]:
    """Trade ticks with trade time at or before ``before``, ascending.

    With ``start``, only ticks at or after ``start`` are read. With
    ``limit``, returns the *latest* ``limit`` ticks of the window (fetched
    descending, then reversed) instead of the whole window.
    """
    where = [
        TradeTickRow.exchange == exchange,
        TradeTickRow.pair == pair,
        TradeTickRow.ts <= before,
    ]
    if start is not None:
        where.append(TradeTickRow.ts >= start)
    if limit is not None:
        stmt = select(TradeTickRow).where(*where).order_by(TradeTickRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(TradeTickRow).where(*where).order_by(TradeTickRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_latest_trade_ts(session: AsyncSession, exchange: str, pair: str) -> datetime | None:
    stmt = (
        select(TradeTickRow.ts)
        .where(
            TradeTickRow.exchange == exchange,
            TradeTickRow.pair == pair,
        )
        .order_by(TradeTickRow.ts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_trade_range(
    session: AsyncSession, exchange: str, pair: str
) -> tuple[datetime | None, datetime | None, int]:
    """(first ts, last ts, count) for coverage reporting."""
    stmt = select(func.min(TradeTickRow.ts), func.max(TradeTickRow.ts), func.count()).where(
        TradeTickRow.exchange == exchange,
        TradeTickRow.pair == pair,
    )
    result = await session.execute(stmt)
    first, last, count = result.one()
    return first, last, count


async def prune_trade_ticks(session: AsyncSession, exchange: str, pair: str, older_than: datetime) -> int:
    """Delete the pair's ticks older than ``older_than``. Returns the deleted count."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(TradeTickRow).where(
                TradeTickRow.exchange == exchange,
                TradeTickRow.pair == pair,
                TradeTickRow.ts < older_than,
            )
        ),
    )
    return result.rowcount
