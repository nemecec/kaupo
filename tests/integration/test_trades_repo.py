"""Trade-tick repository against real Postgres (testcontainers, migrated)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.trades import (
    get_latest_trade_ts,
    get_trade_range,
    get_trade_ticks,
    prune_trade_ticks,
    upsert_trade_ticks,
)
from kaupo.domain import TradeTick

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_tick(
    ts: datetime,
    price: float = 100.0,
    size: float = 0.1,
    side: str = "buy",
    exchange: str = "kraken",
    pair: str = "BTC/EUR",
) -> TradeTick:
    return TradeTick(exchange=exchange, pair=pair, ts=ts, price=price, size=size, side=side)


async def test_migration_created_trade_ticks_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "trade_ticks" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    ticks = [make_tick(BASE + timedelta(minutes=i), price=100.0 + i) for i in range(10)]
    assert await upsert_trade_ticks(session, ticks) == 10

    loaded = await get_trade_ticks(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=10))
    assert len(loaded) == 10
    assert [t.ts for t in loaded] == sorted(t.ts for t in ticks)  # ascending
    assert loaded[5].price == 105.0

    latest = await get_latest_trade_ts(session, "kraken", "BTC/EUR")
    assert latest == BASE + timedelta(minutes=9)

    first, last, count = await get_trade_range(session, "kraken", "BTC/EUR")
    assert (first, last, count) == (BASE, BASE + timedelta(minutes=9), 10)


async def test_upsert_is_idempotent(session: AsyncSession) -> None:
    ticks = [make_tick(BASE + timedelta(minutes=i), price=100.0 + i) for i in range(5)]
    await upsert_trade_ticks(session, ticks)
    await upsert_trade_ticks(session, ticks)  # the same batch again

    _, _, count = await get_trade_range(session, "kraken", "BTC/EUR")
    assert count == 5  # the rerun inserted nothing


async def test_same_ms_identical_ticks_collapse(session: AsyncSession) -> None:
    tick = make_tick(BASE)
    # Kraken serves no trade id: identical same-ms ticks share the full key
    await upsert_trade_ticks(session, [tick, tick])

    loaded = await get_trade_ticks(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=1))
    assert loaded == [tick]

    # a same-ms tick with a different size is a different row
    other = make_tick(BASE, size=0.2)
    await upsert_trade_ticks(session, [other])
    _, _, count = await get_trade_range(session, "kraken", "BTC/EUR")
    assert count == 2


async def test_query_respects_bounds_exchange_and_pair(session: AsyncSession) -> None:
    await upsert_trade_ticks(session, [make_tick(BASE + timedelta(minutes=i)) for i in range(5)])
    await upsert_trade_ticks(session, [make_tick(BASE, price=200.0, pair="ETH/EUR")])
    await upsert_trade_ticks(session, [make_tick(BASE, price=300.0, exchange="binance")])

    end = BASE + timedelta(minutes=10)
    # [start, end): the tick at exactly `end` of a 2-tick window is excluded
    loaded = await get_trade_ticks(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=2))
    assert len(loaded) == 2

    eth = await get_trade_ticks(session, "kraken", "ETH/EUR", BASE, end)
    assert [t.price for t in eth] == [200.0]
    binance = await get_trade_ticks(session, "binance", "BTC/EUR", BASE, end)
    assert [t.price for t in binance] == [300.0]
    assert await get_trade_ticks(session, "kraken", "SOL/EUR", BASE, end) == []


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_trade_ticks(session, [make_tick(BASE + timedelta(minutes=i)) for i in range(5)])

    loaded = await get_trade_ticks(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=5), limit=2)
    assert [t.ts for t in loaded] == [BASE + timedelta(minutes=3), BASE + timedelta(minutes=4)]


async def test_prune_deletes_old_rows_and_keeps_the_window(session: AsyncSession) -> None:
    old = [make_tick(BASE + timedelta(minutes=i)) for i in range(3)]
    recent = [make_tick(BASE + timedelta(days=40, minutes=i)) for i in range(2)]
    other_pair = make_tick(BASE, pair="ETH/EUR")
    await upsert_trade_ticks(session, [*old, *recent, other_pair])

    cutoff = BASE + timedelta(days=30)
    pruned = await prune_trade_ticks(session, "kraken", "BTC/EUR", cutoff)
    assert pruned == 3

    loaded = await get_trade_ticks(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(days=60))
    assert [t.ts for t in loaded] == [t.ts for t in recent]  # the window is kept

    # other pairs are not touched
    eth = await get_trade_ticks(session, "kraken", "ETH/EUR", BASE, BASE + timedelta(days=60))
    assert len(eth) == 1

    # pruning again is a no-op
    assert await prune_trade_ticks(session, "kraken", "BTC/EUR", cutoff) == 0
