"""Futures-metrics daily storage: bulk upsert and range/latest queries."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import FuturesMetricsDailyRow
from kaupo.domain import FuturesMetricsDaily

# the metrics archive is Binance-only (Kraken futures metrics are out of scope)
METRICS_EXCHANGE = "binance"


def _to_domain(row: FuturesMetricsDailyRow) -> FuturesMetricsDaily:
    return FuturesMetricsDaily(
        exchange=row.exchange,
        base_asset=row.base_asset,
        day=row.day,
        oi_base=row.oi_base,
        oi_quote=row.oi_quote,
        count_toptrader_ls_ratio=row.count_toptrader_ls_ratio,
        sum_toptrader_ls_ratio=row.sum_toptrader_ls_ratio,
        count_ls_ratio=row.count_ls_ratio,
        taker_ls_vol_ratio=row.taker_ls_vol_ratio,
    )


async def upsert_futures_metrics_daily(session: AsyncSession, rows: list[FuturesMetricsDaily]) -> int:
    """Idempotent insert; existing (exchange, base_asset, day) rows get the new values."""
    if not rows:
        return 0
    values = [
        {
            "exchange": r.exchange,
            "base_asset": r.base_asset,
            "day": r.day,
            "oi_base": r.oi_base,
            "oi_quote": r.oi_quote,
            "count_toptrader_ls_ratio": r.count_toptrader_ls_ratio,
            "sum_toptrader_ls_ratio": r.sum_toptrader_ls_ratio,
            "count_ls_ratio": r.count_ls_ratio,
            "taker_ls_vol_ratio": r.taker_ls_vol_ratio,
        }
        for r in rows
    ]
    stmt = insert(FuturesMetricsDailyRow).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="futures_metrics_daily_pkey",
        set_={
            "oi_base": stmt.excluded.oi_base,
            "oi_quote": stmt.excluded.oi_quote,
            "count_toptrader_ls_ratio": stmt.excluded.count_toptrader_ls_ratio,
            "sum_toptrader_ls_ratio": stmt.excluded.sum_toptrader_ls_ratio,
            "count_ls_ratio": stmt.excluded.count_ls_ratio,
            "taker_ls_vol_ratio": stmt.excluded.taker_ls_vol_ratio,
        },
    )
    await session.execute(stmt)
    return len(values)


async def get_futures_metrics_daily(
    session: AsyncSession,
    exchange: str,
    base_asset: str,
    start: date,
    end: date,
    limit: int | None = None,
) -> list[FuturesMetricsDaily]:
    """Daily metrics rows with day in [start, end), ascending.

    With ``limit``, returns the *latest* ``limit`` rows of the range
    (fetched descending, then reversed) instead of the whole range.
    """
    where = (
        FuturesMetricsDailyRow.exchange == exchange,
        FuturesMetricsDailyRow.base_asset == base_asset,
        FuturesMetricsDailyRow.day >= start,
        FuturesMetricsDailyRow.day < end,
    )
    if limit is not None:
        stmt = (
            select(FuturesMetricsDailyRow)
            .where(*where)
            .order_by(FuturesMetricsDailyRow.day.desc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
    else:
        stmt = select(FuturesMetricsDailyRow).where(*where).order_by(FuturesMetricsDailyRow.day)
        rows = list((await session.execute(stmt)).scalars())
    return [_to_domain(row) for row in rows]


async def get_latest_futures_metrics_daily(
    session: AsyncSession, exchange: str, base_asset: str, n: int, before_day: date
) -> list[FuturesMetricsDaily]:
    """The ``n`` most recent rows with day strictly before ``before_day``, oldest first.

    The strict bound keeps the in-progress day invisible: only fully closed
    UTC days are ever served.
    """
    stmt = (
        select(FuturesMetricsDailyRow)
        .where(
            FuturesMetricsDailyRow.exchange == exchange,
            FuturesMetricsDailyRow.base_asset == base_asset,
            FuturesMetricsDailyRow.day < before_day,
        )
        .order_by(FuturesMetricsDailyRow.day.desc())
        .limit(n)
    )
    rows = list((await session.execute(stmt)).scalars())
    rows.reverse()
    return [_to_domain(row) for row in rows]


async def get_futures_metrics_range(
    session: AsyncSession, exchange: str, base_asset: str
) -> tuple[date | None, date | None, int]:
    """(first day, last day, count) for coverage reporting."""
    stmt = select(
        func.min(FuturesMetricsDailyRow.day), func.max(FuturesMetricsDailyRow.day), func.count()
    ).where(
        FuturesMetricsDailyRow.exchange == exchange,
        FuturesMetricsDailyRow.base_asset == base_asset,
    )
    result = await session.execute(stmt)
    first, last, count = result.one()
    return first, last, count
