"""Persisted backtest: ctx.funding() serves the DB-seeded series point-in-time."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.data.funding import upsert_funding_rates
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, FundingRate, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

# funding points straddling the hourly candle-close grid; the last one is
# beyond the run's end and must never be served
FUNDING = [
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=2), rate=0.0001),
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=5, minutes=30), rate=-0.0002),
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=100), rate=0.0004),
]

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase

    class FundingRecorder(StrategyBase):
        id = "funding-recorder"
        seen = []  # class-level: the test reads it after the run
        def __init__(self, params):
            super().__init__(params)
        def on_candle(self, ctx):
            type(self).seen.append([(r.ts.isoformat(), r.rate) for r in ctx.funding(50)])
            return []
    """
)


async def test_backtest_funding_is_point_in_time_from_db(session: AsyncSession, tmp_path: Path) -> None:
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
        for i in range(12)
    ]
    await upsert_candles(session, candles)
    await upsert_funding_rates(session, FUNDING)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["funding-recorder"]

    run_id, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
        ),
        get_sessionmaker(),
    )

    assert result.num_fills == 0
    assert run_id is not None

    seen = strategy.cls.seen
    assert len(seen) == 12
    # per candle i (close = BASE + (i+1)h): only funding at or before the close
    expected = [
        [(r.ts.isoformat(), r.rate) for r in FUNDING if r.ts <= BASE + timedelta(hours=i + 1)]
        for i in range(12)
    ]
    assert seen == expected
    # the straddling points show up exactly when the clock crosses them
    assert [len(s) for s in seen] == [0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2]
