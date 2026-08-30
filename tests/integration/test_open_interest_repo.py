"""Open-interest repository against real Postgres (migrated)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.open_interest import get_oi_range, get_open_interest, upsert_open_interest
from kaupo.domain import OpenInterest

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_row(
    hours: float, oi_base: float = 100.0, exchange: str = "binance", base_asset: str = "BTC"
) -> OpenInterest:
    return OpenInterest(
        exchange=exchange,
        base_asset=base_asset,
        ts=BASE + timedelta(hours=hours),
        oi_base=oi_base,
        oi_quote=oi_base * 50_000.0,
    )


async def test_migration_created_open_interest_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "open_interest" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    rows = [make_row(h, oi_base=100.0 + h) for h in range(5)]
    assert await upsert_open_interest(session, rows) == 5

    loaded = await get_open_interest(session, "binance", "BTC", BASE, BASE + timedelta(hours=5))
    assert loaded == rows  # ascending, exact round-trip

    first, last, count = await get_oi_range(session, "binance", "BTC")
    assert (first, last, count) == (BASE, BASE + timedelta(hours=4), 5)


async def test_upsert_updates_the_existing_row(session: AsyncSession) -> None:
    await upsert_open_interest(session, [make_row(0, oi_base=100.0)])
    await upsert_open_interest(session, [make_row(0, oi_base=200.0)])  # same key, new values

    loaded = await get_open_interest(session, "binance", "BTC", BASE, BASE + timedelta(hours=1))
    assert len(loaded) == 1
    assert loaded[0].oi_base == 200.0  # the second upsert's values win


async def test_query_respects_bounds_exchange_and_base_asset(session: AsyncSession) -> None:
    await upsert_open_interest(session, [make_row(h) for h in range(5)])
    await upsert_open_interest(session, [make_row(0, oi_base=1.0, base_asset="ETH")])
    await upsert_open_interest(session, [make_row(0, oi_base=2.0, exchange="kraken")])

    # [start, end): the row of the end hour is excluded
    loaded = await get_open_interest(session, "binance", "BTC", BASE, BASE + timedelta(hours=2))
    assert [r.ts for r in loaded] == [BASE, BASE + timedelta(hours=1)]

    eth = await get_open_interest(session, "binance", "ETH", BASE, BASE + timedelta(hours=5))
    assert [r.oi_base for r in eth] == [1.0]
    kraken = await get_open_interest(session, "kraken", "BTC", BASE, BASE + timedelta(hours=5))
    assert [r.oi_base for r in kraken] == [2.0]
    assert await get_open_interest(session, "binance", "SOL", BASE, BASE + timedelta(hours=5)) == []


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_open_interest(session, [make_row(h) for h in range(5)])

    loaded = await get_open_interest(session, "binance", "BTC", BASE, BASE + timedelta(hours=5), limit=2)
    assert [r.ts for r in loaded] == [BASE + timedelta(hours=3), BASE + timedelta(hours=4)]
