"""Daily order-flow repository and rollup against real Postgres (migrated)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.book import upsert_book_snapshots
from kaupo.data.orderflow_daily import (
    get_latest_orderflow_day,
    get_orderflow_daily,
    list_orderflow_source_pairs,
    rollup_orderflow_daily,
    upsert_orderflow_daily,
)
from kaupo.data.trades import upsert_trade_ticks
from kaupo.domain import BookSnapshot, OrderflowDaily, TradeTick

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY = date(2026, 1, 1)


def make_row(
    day: date,
    trade_count: int = 10,
    exchange: str = "kraken",
    pair: str = "BTC/EUR",
    spread: tuple[float, float] | None = (5.0, 9.0),
) -> OrderflowDaily:
    return OrderflowDaily(
        exchange=exchange,
        pair=pair,
        day=day,
        trade_count=trade_count,
        buy_count=6,
        sell_count=4,
        buy_volume=3.0,
        sell_volume=2.0,
        max_trade_size=1.5,
        book_snapshots=24 if spread else 0,
        spread_mean_bps=spread[0] if spread else None,
        spread_max_bps=spread[1] if spread else None,
    )


def tick(hours: float, side: str, size: float, exchange: str = "kraken", pair: str = "BTC/EUR") -> TradeTick:
    return TradeTick(
        exchange=exchange,
        pair=pair,
        ts=BASE + timedelta(hours=hours),
        price=100.0,
        size=size,
        side=side,
    )


def snapshot(hours: float, bid: float, ask: float, pair: str = "BTC/EUR") -> BookSnapshot:
    return BookSnapshot(
        exchange="kraken",
        pair=pair,
        ts=BASE + timedelta(hours=hours),
        bid=bid,
        ask=ask,
        bid_size=1.0,
        ask_size=2.0,
    )


async def test_migration_created_orderflow_daily_table(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    tables = {r[0] for r in result}
    assert "orderflow_daily" in tables


async def test_upsert_and_query(session: AsyncSession) -> None:
    rows = [make_row(DAY + timedelta(days=i), trade_count=10 * (i + 1)) for i in range(5)]
    assert await upsert_orderflow_daily(session, rows) == 5

    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=5))
    assert loaded == rows  # ascending, exact round-trip including null spreads

    latest = await get_latest_orderflow_day(session, "kraken", "BTC/EUR")
    assert latest == DAY + timedelta(days=4)


async def test_upsert_updates_the_existing_row(session: AsyncSession) -> None:
    await upsert_orderflow_daily(session, [make_row(DAY, trade_count=10, spread=None)])
    await upsert_orderflow_daily(session, [make_row(DAY, trade_count=99)])  # same key, new aggregates

    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1))
    assert len(loaded) == 1
    assert loaded[0].trade_count == 99
    assert loaded[0].spread_mean_bps == 5.0  # the second upsert's values win


async def test_query_respects_bounds_exchange_and_pair(session: AsyncSession) -> None:
    await upsert_orderflow_daily(session, [make_row(DAY + timedelta(days=i)) for i in range(5)])
    await upsert_orderflow_daily(session, [make_row(DAY, trade_count=1, pair="ETH/EUR")])
    await upsert_orderflow_daily(session, [make_row(DAY, trade_count=2, exchange="binance")])

    # [start, end): the row of the end day is excluded
    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=2))
    assert [r.day for r in loaded] == [DAY, DAY + timedelta(days=1)]

    eth = await get_orderflow_daily(session, "kraken", "ETH/EUR", DAY, DAY + timedelta(days=5))
    assert [r.trade_count for r in eth] == [1]
    binance = await get_orderflow_daily(session, "binance", "BTC/EUR", DAY, DAY + timedelta(days=5))
    assert [r.trade_count for r in binance] == [2]
    assert await get_orderflow_daily(session, "kraken", "SOL/EUR", DAY, DAY + timedelta(days=5)) == []
    assert await get_latest_orderflow_day(session, "kraken", "SOL/EUR") is None


async def test_limit_returns_latest_of_range(session: AsyncSession) -> None:
    await upsert_orderflow_daily(session, [make_row(DAY + timedelta(days=i)) for i in range(5)])

    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=5), limit=2)
    assert [r.day for r in loaded] == [DAY + timedelta(days=3), DAY + timedelta(days=4)]


async def test_list_orderflow_source_pairs(session: AsyncSession) -> None:
    await upsert_trade_ticks(session, [tick(1, "buy", 1.0)])
    await upsert_book_snapshots(session, [snapshot(2, 100.0, 101.0, pair="ETH/EUR")])
    await upsert_trade_ticks(session, [tick(3, "sell", 1.0, exchange="binance", pair="SOL/EUR")])

    assert await list_orderflow_source_pairs(session, "kraken") == ["BTC/EUR", "ETH/EUR"]
    assert await list_orderflow_source_pairs(session, "binance") == ["SOL/EUR"]
    assert await list_orderflow_source_pairs(session, "coinbase") == []


async def test_rollup_exact_aggregates(session: AsyncSession) -> None:
    ticks = [
        tick(-0.01, "buy", 9.0),  # the day before: excluded
        tick(0.5, "buy", 1.0),
        tick(5, "buy", 2.0),
        tick(12, "sell", 3.5),
        tick(23.99, "sell", 0.5),
        tick(24, "buy", 9.0),  # exactly the next day's open: excluded ([start, end))
    ]
    book = [
        snapshot(1, 100.0, 101.0),
        snapshot(2, 100.0, 102.0),
        snapshot(25, 100.0, 103.0),  # the next day: excluded
    ]
    await upsert_trade_ticks(session, ticks)
    await upsert_book_snapshots(session, book)

    row = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY)

    assert row.exchange == "kraken"
    assert row.pair == "BTC/EUR"
    assert row.day == DAY
    assert row.trade_count == 4
    assert row.buy_count == 2
    assert row.sell_count == 2
    assert row.buy_volume == pytest.approx(3.0)
    assert row.sell_volume == pytest.approx(4.0)
    assert row.max_trade_size == pytest.approx(3.5)
    assert row.book_snapshots == 2
    spread_1 = (101.0 - 100.0) / ((101.0 + 100.0) / 2) * 10000.0
    spread_2 = (102.0 - 100.0) / ((102.0 + 100.0) / 2) * 10000.0
    assert row.spread_mean_bps == pytest.approx((spread_1 + spread_2) / 2)
    assert row.spread_max_bps == pytest.approx(spread_2)


async def test_rollup_without_book_rows_has_null_spread(session: AsyncSession) -> None:
    await upsert_trade_ticks(session, [tick(1, "buy", 1.0), tick(2, "sell", 2.0)])

    row = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY)

    assert row.trade_count == 2
    assert row.book_snapshots == 0
    assert row.spread_mean_bps is None
    assert row.spread_max_bps is None


async def test_rollup_empty_day_is_all_zero(session: AsyncSession) -> None:
    row = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY)

    assert row.trade_count == 0
    assert row.buy_count == 0
    assert row.sell_count == 0
    assert row.buy_volume == 0.0
    assert row.sell_volume == 0.0
    assert row.max_trade_size == 0.0
    assert row.book_snapshots == 0
    assert row.spread_mean_bps is None
    assert row.spread_max_bps is None


async def test_rollup_rerun_is_idempotent(session: AsyncSession) -> None:
    await upsert_trade_ticks(session, [tick(1, "buy", 1.0), tick(2, "sell", 2.0)])
    await upsert_book_snapshots(session, [snapshot(3, 100.0, 101.0)])

    for _ in range(2):  # the cron reruns the same day: recompute and upsert
        row = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY)
        await upsert_orderflow_daily(session, [row])
        await session.commit()

    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1))
    assert loaded == [row]  # one row, exact aggregates
    assert await get_latest_orderflow_day(session, "kraken", "BTC/EUR") == DAY


async def test_session_basis_persists_and_survives_unscheduled_reruns(session: AsyncSession) -> None:
    from dataclasses import replace

    from kaupo.market_calendar import TradingSchedule

    schedule = TradingSchedule.model_validate({"timezone": "UTC", "week": {0: [{"start": 0, "end": 1440}]}})
    # Thursday is closed, but an unexpected observation must stay in the row.
    await upsert_trade_ticks(session, [tick(1, "buy", 2.0)])
    row = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY, schedule)
    assert row.session_state == "scheduled_closed"
    assert row.trade_count == 1
    await upsert_orderflow_daily(session, [row])
    await session.commit()
    assert await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1)) == [row]

    # A normal cron rerun uses this day's recorded calendar.
    rerun = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY)
    assert rerun == row
    # A direct import without metadata preserves the recorded basis too.
    await upsert_orderflow_daily(
        session, [replace(row, trade_count=2, trading_schedule=None, scheduled_open_seconds=None)]
    )
    await session.commit()
    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1))
    assert loaded == [replace(row, trade_count=2)]

    # An explicit replacement can change the interpretation without changing trades.
    replacement = TradingSchedule.model_validate(
        {"timezone": "UTC", "week": {3: [{"start": 540, "end": 600}]}}
    )
    changed = await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY, replacement)
    await upsert_orderflow_daily(session, [changed])
    await session.commit()
    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1))
    assert loaded == [changed]
    assert loaded[0].session_state == "scheduled_open"
    assert loaded[0].scheduled_open_seconds == 3600
    assert loaded[0].trade_count == 1

    # Neither other pairs/exchanges nor adjacent days inherit this schedule.
    for exchange, pair, day in [
        ("kraken", "ETH/EUR", DAY),
        ("binance", "BTC/EUR", DAY),
        ("kraken", "BTC/EUR", DAY + timedelta(days=1)),
    ]:
        unrelated = await rollup_orderflow_daily(session, exchange, pair, day)
        assert unrelated.session_state == "unknown"


async def test_legacy_insert_and_zero_observation_sessions(session: AsyncSession) -> None:
    from kaupo.market_calendar import TradingSchedule

    # A writer using the old column set remains valid after migration.
    await session.execute(
        text("""
        INSERT INTO orderflow_daily
        (exchange, pair, day, trade_count, buy_count, sell_count, buy_volume,
         sell_volume, max_trade_size, book_snapshots)
        VALUES ('kraken', 'BTC/EUR', '2026-01-01', 0, 0, 0, 0, 0, 0, 0)
    """)
    )
    (legacy,) = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=1))
    assert legacy.session_state == "unknown"
    assert legacy.trading_schedule is None
    schedule = TradingSchedule.model_validate(
        {"timezone": "UTC", "week": {day: [{"start": 0, "end": 1440}] for day in range(5)}}
    )
    rows = [
        await rollup_orderflow_daily(session, "kraken", "BTC/EUR", DAY + timedelta(days=i), schedule)
        for i in range(4)
    ]
    await upsert_orderflow_daily(session, rows)
    await session.commit()
    loaded = await get_orderflow_daily(session, "kraken", "BTC/EUR", DAY, DAY + timedelta(days=4))
    assert loaded == rows
    assert [row.session_state for row in loaded] == [
        "scheduled_open",
        "scheduled_open",
        "scheduled_closed",
        "scheduled_closed",
    ]
    assert [row.scheduled_open_seconds for row in loaded] == [86400, 86400, 0, 0]
    assert all(row.trade_count == row.book_snapshots == 0 for row in loaded)
