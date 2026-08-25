"""Funding-rate repository against real Postgres (testcontainers, migrated)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.funding import (
    get_funding_range,
    get_funding_rates,
    get_latest_funding_rates,
    get_latest_funding_ts,
    upsert_funding_rates,
)
from kaupo.domain import FundingRate

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_rate(
    ts: datetime, rate: float = 0.0001, exchange: str = "binance", base: str = "BTC"
) -> FundingRate:
    return FundingRate(exchange=exchange, base_asset=base, ts=ts, rate=rate)


async def test_migration_created_funding_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "funding_rates" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    rates = [make_rate(BASE + timedelta(hours=8 * i), 0.0001 * i) for i in range(10)]
    assert await upsert_funding_rates(session, rates) == 10

    loaded = await get_funding_rates(session, "binance", "BTC", BASE, BASE + timedelta(hours=80))
    assert len(loaded) == 10
    assert [r.ts for r in loaded] == sorted(r.ts for r in rates)  # ascending
    assert loaded[5].rate == 0.0005

    latest = await get_latest_funding_ts(session, "binance", "BTC")
    assert latest == BASE + timedelta(hours=72)

    first, last, count = await get_funding_range(session, "binance", "BTC")
    assert (first, last, count) == (BASE, BASE + timedelta(hours=72), 10)


async def test_upsert_is_idempotent_and_updates_rate(session: AsyncSession) -> None:
    await upsert_funding_rates(session, [make_rate(BASE, 0.0001)])
    await upsert_funding_rates(session, [make_rate(BASE, 0.0002)])  # same ts, new rate

    loaded = await get_funding_rates(session, "binance", "BTC", BASE, BASE + timedelta(hours=1))
    assert len(loaded) == 1
    assert loaded[0].rate == 0.0002


async def test_query_respects_bounds_exchange_and_base(session: AsyncSession) -> None:
    await upsert_funding_rates(session, [make_rate(BASE + timedelta(hours=8 * i)) for i in range(5)])
    await upsert_funding_rates(session, [make_rate(BASE, 0.0099, base="SOL")])
    await upsert_funding_rates(session, [make_rate(BASE, 0.0088, exchange="kraken")])

    end = BASE + timedelta(hours=80)
    # [start, end): the point at exactly `end` of a 2-point window is excluded
    loaded = await get_funding_rates(session, "binance", "BTC", BASE, BASE + timedelta(hours=16))
    assert len(loaded) == 2

    assert await get_funding_rates(session, "binance", "SOL", BASE, end) == [
        make_rate(BASE, 0.0099, base="SOL")
    ]
    assert await get_funding_rates(session, "kraken", "BTC", BASE, end) == [
        make_rate(BASE, 0.0088, exchange="kraken")
    ]
    assert await get_funding_rates(session, "binance", "ETH", BASE, end) == []


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_funding_rates(session, [make_rate(BASE + timedelta(hours=8 * i)) for i in range(5)])

    loaded = await get_funding_rates(session, "binance", "BTC", BASE, BASE + timedelta(hours=80), limit=2)
    assert [r.ts for r in loaded] == [BASE + timedelta(hours=24), BASE + timedelta(hours=32)]


async def test_latest_n_at_or_before(session: AsyncSession) -> None:
    await upsert_funding_rates(session, [make_rate(BASE + timedelta(hours=8 * i)) for i in range(5)])

    # `before` is inclusive; result ascending
    loaded = await get_latest_funding_rates(session, "binance", "BTC", 2, before=BASE + timedelta(hours=16))
    assert [r.ts for r in loaded] == [BASE + timedelta(hours=8), BASE + timedelta(hours=16)]

    nothing = await get_latest_funding_rates(session, "binance", "BTC", 2, before=BASE - timedelta(seconds=1))
    assert nothing == []
