from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.candles import get_candle_range, get_candles, get_latest_ts, upsert_candles
from kaupo.domain import Candle, Pair, Timeframe

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")


def make_candle(ts: datetime, price: float = 100.0) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=ts,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price * 1.005,
        volume=12.5,
    )


async def test_migration_created_tables(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert {
        "candles",
        "strategies",
        "runs",
        "orders",
        "fills",
        "ledger_entries",
        "equity_snapshots",
        "reports",
        "events",
    } <= tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [make_candle(base + timedelta(hours=i), 100 + i) for i in range(10)]
    assert await upsert_candles(session, candles) == 10

    loaded = await get_candles(session, PAIR, Timeframe.H1, base, base + timedelta(hours=10))
    assert len(loaded) == 10
    assert loaded[0].ts == base
    assert loaded[5].close == pytest.approx((105) * 1.005)

    latest = await get_latest_ts(session, PAIR, Timeframe.H1)
    assert latest == base + timedelta(hours=9)

    first, last, count = await get_candle_range(session, PAIR, Timeframe.H1)
    assert first == base
    assert last == base + timedelta(hours=9)
    assert count == 10


async def test_upsert_is_idempotent_and_updates(session: AsyncSession) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await upsert_candles(session, [make_candle(base, 100)])
    await upsert_candles(session, [make_candle(base, 200)])  # same ts, new price

    loaded = await get_candles(session, PAIR, Timeframe.H1, base, base + timedelta(hours=1))
    assert len(loaded) == 1
    assert loaded[0].open == 200.0


async def test_query_respects_bounds_and_pair(session: AsyncSession) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await upsert_candles(session, [make_candle(base + timedelta(hours=i)) for i in range(5)])

    loaded = await get_candles(session, PAIR, Timeframe.H1, base, base + timedelta(hours=2))
    assert len(loaded) == 2

    other = await get_candles(session, Pair.parse("ETH/EUR"), Timeframe.H1, base, base + timedelta(hours=5))
    assert other == []
