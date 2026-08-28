"""Persisted backtest: ctx.tick_flow_daily() serves the DB-seeded daily
aggregates point-in-time (only fully closed UTC days)."""

import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.data.orderflow_daily import upsert_orderflow_daily
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, OrderflowDaily, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def daily(day: date, trade_count: int) -> OrderflowDaily:
    return OrderflowDaily(
        exchange="kraken",
        pair=str(PAIR),
        day=day,
        trade_count=trade_count,
        buy_count=6,
        sell_count=4,
        buy_volume=3.0,
        sell_volume=2.0,
        max_trade_size=1.5,
        book_snapshots=24,
        spread_mean_bps=5.0,
        spread_max_bps=9.0,
    )


# one row per day; the last one closes after the run ends and must never be served
DAILY = [
    daily(date(2026, 1, 1), 10),
    daily(date(2026, 1, 2), 20),
    daily(date(2026, 1, 3), 30),
    daily(date(2026, 1, 4), 40),
]

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase

    class DailyFlowRecorder(StrategyBase):
        id = "daily-flow-recorder"
        daily_seen = []  # class-level: the test reads it after the run
        def __init__(self, params):
            super().__init__(params)
        def on_candle(self, ctx):
            type(self).daily_seen.append([
                (r.day.isoformat(), r.trade_count, r.buy_count, r.buy_volume, r.spread_mean_bps)
                for r in ctx.tick_flow_daily(10)
            ])
            return []
    """
)

R1 = ("2026-01-01", 10, 6, 3.0, 5.0)
R2 = ("2026-01-02", 20, 6, 3.0, 5.0)


async def test_backtest_tick_flow_daily_is_point_in_time_from_db(
    session: AsyncSession, tmp_path: Path
) -> None:
    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(50)
    ]
    await upsert_candles(session, candles)
    await upsert_orderflow_daily(session, DAILY)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["daily-flow-recorder"]

    run_id, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=50),
        ),
        get_sessionmaker(),
    )

    assert result.num_fills == 0
    assert run_id is not None

    # the day-1 row appears at the first close of day 2 (the day just closed),
    # the day-2 row at the first close of day 3; day 3 is still in progress
    # when the run ends, so its row (and day 4's) never leaks
    expected = [[]] * 23 + [[R1]] * 24 + [[R1, R2]] * 3
    assert strategy.cls.daily_seen == expected
