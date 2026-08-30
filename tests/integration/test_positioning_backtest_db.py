"""Persisted backtest: ctx.open_interest() and ctx.futures_metrics_daily()
serve the DB-seeded positioning series point-in-time (snapshots at or before
the clock; only fully closed UTC days for the daily rows)."""

import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.data.futures_metrics import upsert_futures_metrics_daily
from kaupo.data.open_interest import upsert_open_interest
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, FuturesMetricsDaily, OpenInterest, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY = date(2026, 1, 1)


def oi(hours: float) -> OpenInterest:
    return OpenInterest(
        exchange="binance",
        base_asset="BTC",
        ts=BASE + timedelta(hours=hours),
        oi_base=100.0 + hours,
        oi_quote=(100.0 + hours) * 50_000.0,
    )


# hourly snapshots across the run window; rows after the virtual clock must
# never be served even though the whole window is prefilled
OI_ROWS = [oi(h) for h in range(50)]


def metrics(days: int, value: float) -> FuturesMetricsDaily:
    return FuturesMetricsDaily(
        exchange="binance",
        base_asset="BTC",
        day=DAY + timedelta(days=days),
        oi_base=value,
        oi_quote=value * 50_000.0,
        count_toptrader_ls_ratio=2.0,
        sum_toptrader_ls_ratio=1.5,
        count_ls_ratio=2.5,
        taker_ls_vol_ratio=1.1,
    )


# day 3 and 4 close after the run ends and must never be served
METRIC_ROWS = [metrics(0, 10.0), metrics(1, 20.0), metrics(2, 30.0), metrics(3, 40.0)]

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase

    class PositioningRecorder(StrategyBase):
        id = "positioning-recorder"
        oi_seen = []  # class-level: the test reads it after the run
        metrics_seen = []
        def __init__(self, params):
            super().__init__(params)
        def on_candle(self, ctx):
            type(self).oi_seen.append([r.oi_base for r in ctx.open_interest(10)])
            type(self).metrics_seen.append([r.oi_base for r in ctx.futures_metrics_daily(10)])
            return []
    """
)


async def test_backtest_positioning_series_are_point_in_time_from_db(
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
    await upsert_open_interest(session, OI_ROWS)
    await upsert_futures_metrics_daily(session, METRIC_ROWS)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["positioning-recorder"]

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

    oi_seen = strategy.cls.oi_seen
    metrics_seen = strategy.cls.metrics_seen
    assert len(oi_seen) == 50
    assert len(metrics_seen) == 50

    # candle i closes at ts+1h: snapshots up to and including that close are
    # visible (bisect boundary is inclusive); later snapshots never leak
    assert oi_seen[0] == [100.0, 101.0]  # hours 0 and 1, hours 2+ not yet visible
    assert oi_seen[8] == [100.0 + h for h in range(0, 10)][-10:]  # hours 0..9
    # the newest ten at the final close: hours 40..49
    assert oi_seen[49] == [100.0 + h for h in range(40, 50)]

    # the day-1 row appears at the first close of day 2 (candle 23), the day-2
    # row at the first close of day 3 (candle 47); days 3 and 4 never leak
    assert metrics_seen[0] == []
    assert metrics_seen[22] == []
    assert metrics_seen[23] == [10.0]
    assert metrics_seen[46] == [10.0]
    assert metrics_seen[47] == [10.0, 20.0]
    assert metrics_seen[49] == [10.0, 20.0]
