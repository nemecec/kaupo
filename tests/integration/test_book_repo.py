"""Book-snapshot repository against real Postgres (testcontainers, migrated)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.book import (
    get_book_snapshots,
    get_latest_book_ts,
    prune_book_snapshots,
    upsert_book_snapshots,
)
from kaupo.domain import BookSnapshot

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_snapshot(
    ts: datetime,
    bid: float = 100.0,
    ask: float = 100.5,
    bid_size: float = 1.0,
    ask_size: float = 2.0,
    exchange: str = "kraken",
    pair: str = "BTC/EUR",
) -> BookSnapshot:
    return BookSnapshot(
        exchange=exchange, pair=pair, ts=ts, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size
    )


async def test_migration_created_book_snapshots_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "book_snapshots" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    snapshots = [make_snapshot(BASE + timedelta(minutes=i), bid=100.0 + i) for i in range(10)]
    assert await upsert_book_snapshots(session, snapshots) == 10

    loaded = await get_book_snapshots(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=10))
    assert len(loaded) == 10
    assert [s.ts for s in loaded] == sorted(s.ts for s in snapshots)  # ascending
    assert loaded[5].bid == 105.0
    assert (loaded[5].ask, loaded[5].bid_size, loaded[5].ask_size) == (100.5, 1.0, 2.0)

    latest = await get_latest_book_ts(session, "kraken", "BTC/EUR")
    assert latest == BASE + timedelta(minutes=9)


async def test_upsert_is_idempotent(session: AsyncSession) -> None:
    snapshots = [make_snapshot(BASE + timedelta(minutes=i), bid=100.0 + i) for i in range(5)]
    await upsert_book_snapshots(session, snapshots)
    await upsert_book_snapshots(session, snapshots)  # the same batch again

    loaded = await get_book_snapshots(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=5))
    assert len(loaded) == 5  # the rerun inserted nothing


async def test_same_observation_collapses_to_one_row(session: AsyncSession) -> None:
    # two polls that see the same ticker timestamp share the key; the first row wins
    first = make_snapshot(BASE, bid=100.0)
    changed = make_snapshot(BASE, bid=101.0)
    await upsert_book_snapshots(session, [first])
    await upsert_book_snapshots(session, [changed])

    loaded = await get_book_snapshots(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=1))
    assert len(loaded) == 1
    assert loaded[0].bid == 100.0


async def test_query_respects_bounds_exchange_and_pair(session: AsyncSession) -> None:
    await upsert_book_snapshots(session, [make_snapshot(BASE + timedelta(minutes=i)) for i in range(5)])
    await upsert_book_snapshots(session, [make_snapshot(BASE, bid=200.0, pair="ETH/EUR")])
    await upsert_book_snapshots(session, [make_snapshot(BASE, bid=300.0, exchange="binance")])

    end = BASE + timedelta(minutes=10)
    # [start, end): the snapshot at exactly `end` of a 2-row window is excluded
    loaded = await get_book_snapshots(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=2))
    assert len(loaded) == 2

    eth = await get_book_snapshots(session, "kraken", "ETH/EUR", BASE, end)
    assert [s.bid for s in eth] == [200.0]
    binance = await get_book_snapshots(session, "binance", "BTC/EUR", BASE, end)
    assert [s.bid for s in binance] == [300.0]
    assert await get_book_snapshots(session, "kraken", "SOL/EUR", BASE, end) == []


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_book_snapshots(session, [make_snapshot(BASE + timedelta(minutes=i)) for i in range(5)])

    loaded = await get_book_snapshots(
        session, "kraken", "BTC/EUR", BASE, BASE + timedelta(minutes=5), limit=2
    )
    assert [s.ts for s in loaded] == [BASE + timedelta(minutes=3), BASE + timedelta(minutes=4)]


async def test_prune_deletes_old_rows_and_keeps_the_window(session: AsyncSession) -> None:
    old = [make_snapshot(BASE + timedelta(minutes=i)) for i in range(3)]
    recent = [make_snapshot(BASE + timedelta(days=40, minutes=i)) for i in range(2)]
    other_pair = make_snapshot(BASE, pair="ETH/EUR")
    await upsert_book_snapshots(session, [*old, *recent, other_pair])

    cutoff = BASE + timedelta(days=30)
    pruned = await prune_book_snapshots(session, "kraken", "BTC/EUR", cutoff)
    assert pruned == 3

    loaded = await get_book_snapshots(session, "kraken", "BTC/EUR", BASE, BASE + timedelta(days=60))
    assert [s.ts for s in loaded] == [s.ts for s in recent]  # the window is kept

    # other pairs are not touched
    eth = await get_book_snapshots(session, "kraken", "ETH/EUR", BASE, BASE + timedelta(days=60))
    assert len(eth) == 1

    # pruning again is a no-op
    assert await prune_book_snapshots(session, "kraken", "BTC/EUR", cutoff) == 0
