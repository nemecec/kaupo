"""Persisted backtest: ctx.ticks()/book()/tick_flow() serve the DB-seeded
series point-in-time."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.book import upsert_book_snapshots
from kaupo.data.candles import upsert_candles
from kaupo.data.trades import upsert_trade_ticks
from kaupo.db.session import get_sessionmaker
from kaupo.domain import BookSnapshot, Candle, Pair, Timeframe, TradeTick
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def tick(minutes: float, side: str, size: float) -> TradeTick:
    return TradeTick(
        exchange="kraken",
        pair=str(PAIR),
        ts=BASE + timedelta(minutes=minutes),
        price=100.0,
        size=size,
        side=side,
    )


def snapshot(minutes: float, bid: float, ask: float) -> BookSnapshot:
    return BookSnapshot(
        exchange="kraken",
        pair=str(PAIR),
        ts=BASE + timedelta(minutes=minutes),
        bid=bid,
        ask=ask,
        bid_size=1.0,
        ask_size=2.0,
    )


# ticks straddling the hourly candle-close grid: 60m lands exactly on a
# close (visible to ticks() at that close, but its flow bucket is still
# open), 600m sits hours later
TICKS = [
    tick(30, "buy", 1.0),
    tick(60, "buy", 0.5),
    tick(61, "sell", 2.0),
    tick(119, "buy", 4.0),
    tick(600, "sell", 7.0),
]
# book snapshots between closes; the last one is beyond the run's end and
# must never be served
BOOK = [snapshot(90, 99.0, 101.0), snapshot(200, 98.0, 102.0), snapshot(6000, 97.0, 103.0)]

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase

    class OrderFlowRecorder(StrategyBase):
        id = "orderflow-recorder"
        ticks_seen = []  # class-level: the test reads them after the run
        book_seen = []
        flow_seen = []
        def __init__(self, params):
            super().__init__(params)
        def on_candle(self, ctx):
            type(self).ticks_seen.append([(t.ts.isoformat(), t.side, t.size) for t in ctx.ticks(50)])
            type(self).book_seen.append([(s.ts.isoformat(), s.bid, s.ask) for s in ctx.book(50)])
            type(self).flow_seen.append([
                (f.ts.isoformat(), f.buy_count, f.sell_count, f.buy_volume, f.sell_volume, f.max_trade_size)
                for f in ctx.tick_flow(50)
            ])
            return []
    """
)

# the three non-empty buckets, as the strategy records them
F0 = (BASE.isoformat(), 1, 0, 1.0, 0.0, 1.0)
F1 = ((BASE + timedelta(hours=1)).isoformat(), 2, 1, 4.5, 2.0, 4.0)
F10 = ((BASE + timedelta(hours=10)).isoformat(), 0, 1, 0.0, 7.0, 7.0)


async def test_backtest_orderflow_is_point_in_time_from_db(session: AsyncSession, tmp_path: Path) -> None:
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
    await upsert_trade_ticks(session, TICKS)
    await upsert_book_snapshots(session, BOOK)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["orderflow-recorder"]

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

    # per candle i (close = BASE + (i+1)h): only rows at or before the close
    expected_ticks = [
        [(t.ts.isoformat(), t.side, t.size) for t in TICKS if t.ts <= BASE + timedelta(hours=i + 1)]
        for i in range(12)
    ]
    assert strategy.cls.ticks_seen == expected_ticks
    # the straddling points show up exactly when the clock crosses them
    assert [len(s) for s in strategy.cls.ticks_seen] == [2, 4, 4, 4, 4, 4, 4, 4, 4, 5, 5, 5]

    expected_book = [
        [(s.ts.isoformat(), s.bid, s.ask) for s in BOOK if s.ts <= BASE + timedelta(hours=i + 1)]
        for i in range(12)
    ]
    assert strategy.cls.book_seen == expected_book
    assert [len(s) for s in strategy.cls.book_seen] == [0, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2]

    # tick_flow: a bucket appears at the close that completes it — the 60m
    # tick joins a flow bucket only at the 2h close, the 600m one at 11h
    expected_flow = [[F0], [F0, F1]] + [[F0, F1]] * 8 + [[F0, F1, F10]] * 2
    assert strategy.cls.flow_seen == expected_flow
