"""Top-of-book storage: bulk upsert, range/latest queries, retention pruning."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import BookSnapshotRow
from kaupo.domain import BookSnapshot


def _to_domain(row: BookSnapshotRow) -> BookSnapshot:
    return BookSnapshot(
        exchange=row.exchange,
        pair=row.pair,
        ts=row.ts,
        bid=row.bid,
        ask=row.ask,
        bid_size=row.bid_size,
        ask_size=row.ask_size,
    )


async def upsert_book_snapshots(session: AsyncSession, snapshots: list[BookSnapshot]) -> int:
    """Insert snapshots; rows already in the table (same key) are skipped.

    Returns the number of snapshots handed to the insert; ON CONFLICT DO
    NOTHING drops duplicates (same exchange, pair, and ts) silently, so the
    inserted count can be lower.
    """
    if not snapshots:
        return 0
    rows = [
        {
            "exchange": s.exchange,
            "pair": s.pair,
            "ts": s.ts,
            "bid": s.bid,
            "ask": s.ask,
            "bid_size": s.bid_size,
            "ask_size": s.ask_size,
        }
        for s in snapshots
    ]
    stmt = insert(BookSnapshotRow).values(rows).on_conflict_do_nothing()
    await session.execute(stmt)
    return len(rows)


async def get_book_snapshots(
    session: AsyncSession,
    exchange: str,
    pair: str,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> list[BookSnapshot]:
    """Book snapshots with observation time in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` snapshots of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        BookSnapshotRow.exchange == exchange,
        BookSnapshotRow.pair == pair,
        BookSnapshotRow.ts >= start,
        BookSnapshotRow.ts < end,
    )
    if limit is not None:
        stmt = select(BookSnapshotRow).where(*where).order_by(BookSnapshotRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(BookSnapshotRow).where(*where).order_by(BookSnapshotRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_recent_book_snapshots(
    session: AsyncSession,
    exchange: str,
    pair: str,
    before: datetime,
    start: datetime | None = None,
    limit: int | None = None,
) -> list[BookSnapshot]:
    """Book snapshots with observation time at or before ``before``, ascending.

    With ``start``, only snapshots at or after ``start`` are read. With
    ``limit``, returns the *latest* ``limit`` snapshots of the window
    (fetched descending, then reversed) instead of the whole window.
    """
    where = [
        BookSnapshotRow.exchange == exchange,
        BookSnapshotRow.pair == pair,
        BookSnapshotRow.ts <= before,
    ]
    if start is not None:
        where.append(BookSnapshotRow.ts >= start)
    if limit is not None:
        stmt = select(BookSnapshotRow).where(*where).order_by(BookSnapshotRow.ts.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(BookSnapshotRow).where(*where).order_by(BookSnapshotRow.ts)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_latest_book_ts(session: AsyncSession, exchange: str, pair: str) -> datetime | None:
    stmt = (
        select(BookSnapshotRow.ts)
        .where(
            BookSnapshotRow.exchange == exchange,
            BookSnapshotRow.pair == pair,
        )
        .order_by(BookSnapshotRow.ts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def prune_book_snapshots(session: AsyncSession, exchange: str, pair: str, older_than: datetime) -> int:
    """Delete the pair's snapshots older than ``older_than``. Returns the deleted count."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(BookSnapshotRow).where(
                BookSnapshotRow.exchange == exchange,
                BookSnapshotRow.pair == pair,
                BookSnapshotRow.ts < older_than,
            )
        ),
    )
    return result.rowcount
