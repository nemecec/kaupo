"""Futures-metrics daily repository against real Postgres (migrated)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.futures_metrics import (
    get_futures_metrics_daily,
    get_futures_metrics_range,
    upsert_futures_metrics_daily,
)
from kaupo.domain import FuturesMetricsDaily

pytestmark = pytest.mark.integration

DAY = date(2026, 1, 1)


def make_row(
    day: date, oi_base: float = 100.0, exchange: str = "binance", base_asset: str = "BTC"
) -> FuturesMetricsDaily:
    return FuturesMetricsDaily(
        exchange=exchange,
        base_asset=base_asset,
        day=day,
        oi_base=oi_base,
        oi_quote=oi_base * 50_000.0,
        count_toptrader_ls_ratio=2.0,
        sum_toptrader_ls_ratio=1.5,
        count_ls_ratio=2.5,
        taker_ls_vol_ratio=1.1,
    )


async def test_migration_created_futures_metrics_daily_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "futures_metrics_daily" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    rows = [make_row(DAY + timedelta(days=i), oi_base=100.0 + i) for i in range(5)]
    assert await upsert_futures_metrics_daily(session, rows) == 5

    loaded = await get_futures_metrics_daily(session, "binance", "BTC", DAY, DAY + timedelta(days=5))
    assert loaded == rows  # ascending, exact round-trip

    first, last, count = await get_futures_metrics_range(session, "binance", "BTC")
    assert (first, last, count) == (DAY, DAY + timedelta(days=4), 5)


async def test_upsert_updates_the_existing_row(session: AsyncSession) -> None:
    await upsert_futures_metrics_daily(session, [make_row(DAY, oi_base=100.0)])
    await upsert_futures_metrics_daily(session, [make_row(DAY, oi_base=200.0)])  # same key, new values

    loaded = await get_futures_metrics_daily(session, "binance", "BTC", DAY, DAY + timedelta(days=1))
    assert len(loaded) == 1
    assert loaded[0].oi_base == 200.0  # the second upsert's values win


async def test_query_respects_bounds_exchange_and_base_asset(session: AsyncSession) -> None:
    await upsert_futures_metrics_daily(session, [make_row(DAY + timedelta(days=i)) for i in range(5)])
    await upsert_futures_metrics_daily(session, [make_row(DAY, oi_base=1.0, base_asset="ETH")])
    await upsert_futures_metrics_daily(session, [make_row(DAY, oi_base=2.0, exchange="kraken")])

    # [start, end): the row of the end day is excluded
    loaded = await get_futures_metrics_daily(session, "binance", "BTC", DAY, DAY + timedelta(days=2))
    assert [r.day for r in loaded] == [DAY, DAY + timedelta(days=1)]

    eth = await get_futures_metrics_daily(session, "binance", "ETH", DAY, DAY + timedelta(days=5))
    assert [r.oi_base for r in eth] == [1.0]
    kraken = await get_futures_metrics_daily(session, "kraken", "BTC", DAY, DAY + timedelta(days=5))
    assert [r.oi_base for r in kraken] == [2.0]
    assert await get_futures_metrics_daily(session, "binance", "SOL", DAY, DAY + timedelta(days=5)) == []


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_futures_metrics_daily(session, [make_row(DAY + timedelta(days=i)) for i in range(5)])

    loaded = await get_futures_metrics_daily(session, "binance", "BTC", DAY, DAY + timedelta(days=5), limit=2)
    assert [r.day for r in loaded] == [DAY + timedelta(days=3), DAY + timedelta(days=4)]
