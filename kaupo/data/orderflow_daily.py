"""Daily order-flow aggregates: rollup from the raw stores, upsert, queries."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import BookSnapshotRow, OrderflowDailyRow, TradeTickRow
from kaupo.domain import OrderflowDaily


def _to_domain(row: OrderflowDailyRow) -> OrderflowDaily:
    return OrderflowDaily(
        exchange=row.exchange,
        pair=row.pair,
        day=row.day,
        trade_count=row.trade_count,
        buy_count=row.buy_count,
        sell_count=row.sell_count,
        buy_volume=row.buy_volume,
        sell_volume=row.sell_volume,
        max_trade_size=row.max_trade_size,
        book_snapshots=row.book_snapshots,
        spread_mean_bps=row.spread_mean_bps,
        spread_max_bps=row.spread_max_bps,
    )


async def upsert_orderflow_daily(session: AsyncSession, rows: list[OrderflowDaily]) -> int:
    """Idempotent insert; existing (exchange, pair, day) rows get the new aggregates."""
    if not rows:
        return 0
    values = [
        {
            "exchange": r.exchange,
            "pair": r.pair,
            "day": r.day,
            "trade_count": r.trade_count,
            "buy_count": r.buy_count,
            "sell_count": r.sell_count,
            "buy_volume": r.buy_volume,
            "sell_volume": r.sell_volume,
            "max_trade_size": r.max_trade_size,
            "book_snapshots": r.book_snapshots,
            "spread_mean_bps": r.spread_mean_bps,
            "spread_max_bps": r.spread_max_bps,
        }
        for r in rows
    ]
    stmt = insert(OrderflowDailyRow).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="orderflow_daily_pkey",
        set_={
            "trade_count": stmt.excluded.trade_count,
            "buy_count": stmt.excluded.buy_count,
            "sell_count": stmt.excluded.sell_count,
            "buy_volume": stmt.excluded.buy_volume,
            "sell_volume": stmt.excluded.sell_volume,
            "max_trade_size": stmt.excluded.max_trade_size,
            "book_snapshots": stmt.excluded.book_snapshots,
            "spread_mean_bps": stmt.excluded.spread_mean_bps,
            "spread_max_bps": stmt.excluded.spread_max_bps,
        },
    )
    await session.execute(stmt)
    return len(values)


async def get_orderflow_daily(
    session: AsyncSession,
    exchange: str,
    pair: str,
    start: date,
    end: date,
    limit: int | None = None,
) -> list[OrderflowDaily]:
    """Daily aggregates with day in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` rows of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        OrderflowDailyRow.exchange == exchange,
        OrderflowDailyRow.pair == pair,
        OrderflowDailyRow.day >= start,
        OrderflowDailyRow.day < end,
    )
    if limit is not None:
        stmt = select(OrderflowDailyRow).where(*where).order_by(OrderflowDailyRow.day.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(OrderflowDailyRow).where(*where).order_by(OrderflowDailyRow.day)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_latest_orderflow_day(session: AsyncSession, exchange: str, pair: str) -> date | None:
    stmt = (
        select(OrderflowDailyRow.day)
        .where(
            OrderflowDailyRow.exchange == exchange,
            OrderflowDailyRow.pair == pair,
        )
        .order_by(OrderflowDailyRow.day.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_orderflow_source_pairs(session: AsyncSession, exchange: str) -> list[str]:
    """Pairs with raw order-flow rows (trade ticks or book snapshots), sorted."""
    combined = union(
        select(TradeTickRow.pair).where(TradeTickRow.exchange == exchange),
        select(BookSnapshotRow.pair).where(BookSnapshotRow.exchange == exchange),
    ).subquery()
    result = await session.execute(select(combined.c.pair).order_by(combined.c.pair))
    return [row[0] for row in result]


async def rollup_orderflow_daily(
    session: AsyncSession, exchange: str, pair: str, day: date
) -> OrderflowDaily:
    """Aggregate the pair's raw order-flow rows of one UTC day.

    One SQL aggregate over the day's trade ticks (counts and base-currency
    volumes per taker side, largest trade) and one over the day's book
    snapshots (count, mean and max of spread_bps = (ask-bid)/mid*10000).
    Spread fields are null when the day has no snapshots. Pure computation;
    persist the result with :func:`upsert_orderflow_daily` (a rerun of the
    same day recomputes and overwrites the same row).
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    is_buy = TradeTickRow.side == "buy"
    is_sell = TradeTickRow.side == "sell"
    trade_stmt = select(
        func.count(),
        func.count().filter(is_buy),
        func.count().filter(is_sell),
        func.coalesce(func.sum(TradeTickRow.size).filter(is_buy), 0.0),
        func.coalesce(func.sum(TradeTickRow.size).filter(is_sell), 0.0),
        func.coalesce(func.max(TradeTickRow.size), 0.0),
    ).where(
        TradeTickRow.exchange == exchange,
        TradeTickRow.pair == pair,
        TradeTickRow.ts >= start,
        TradeTickRow.ts < end,
    )
    trade_count, buy_count, sell_count, buy_volume, sell_volume, max_size = (
        await session.execute(trade_stmt)
    ).one()

    mid = (BookSnapshotRow.bid + BookSnapshotRow.ask) / 2.0
    spread_bps = (BookSnapshotRow.ask - BookSnapshotRow.bid) / mid * 10000.0
    book_stmt = select(
        func.count(),
        func.avg(spread_bps),
        func.max(spread_bps),
    ).where(
        BookSnapshotRow.exchange == exchange,
        BookSnapshotRow.pair == pair,
        BookSnapshotRow.ts >= start,
        BookSnapshotRow.ts < end,
    )
    book_count, spread_mean, spread_max = (await session.execute(book_stmt)).one()

    return OrderflowDaily(
        exchange=exchange,
        pair=pair,
        day=day,
        trade_count=trade_count,
        buy_count=buy_count,
        sell_count=sell_count,
        buy_volume=float(buy_volume),
        sell_volume=float(sell_volume),
        max_trade_size=float(max_size),
        book_snapshots=book_count,
        spread_mean_bps=None if book_count == 0 else float(spread_mean),
        spread_max_bps=None if book_count == 0 else float(spread_max),
    )
