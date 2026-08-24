"""End-to-end backtest with Postgres persistence (testcontainers)."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, RunRow, StrategyRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase
    from kaupo.domain import OrderIntent, Pair, Side

    class BuyAndSell(StrategyBase):
        id = "buy-and-sell"
        def __init__(self, params):
            super().__init__(params)
            self.n = 0
        def on_candle(self, ctx):
            self.n += 1
            pair = ctx.candle.pair
            if self.n == 3:
                return [OrderIntent(pair=pair, side=Side.BUY, size=1.0)]
            if self.n == 7:
                return [OrderIntent(pair=pair, side=Side.SELL, size=1.0)]
            return []
    """
)


async def test_backtest_persists_full_run(session: AsyncSession, tmp_path: Path) -> None:
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
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["buy-and-sell"]

    sessionmaker = get_sessionmaker()
    run_id, result, metrics = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
        ),
        sessionmaker,
    )

    assert result.num_fills == 2
    assert metrics["num_fills"] == 2

    run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
    assert run.mode == "backtest"
    assert run.status == "completed"
    assert run.strategy_id == "buy-and-sell"
    assert run.metrics["num_fills"] == 2
    assert run.ended_at is not None

    strat = (await session.execute(select(StrategyRow))).scalar_one()
    assert strat.id == "buy-and-sell"
    assert strat.source_hash == strategy.source_hash

    orders = (await session.execute(select(OrderRow).where(OrderRow.run_id == run_id))).scalars().all()
    assert len(orders) == 2
    assert all(o.status == "filled" for o in orders)

    fills = (await session.execute(select(FillRow).where(FillRow.run_id == run_id))).scalars().all()
    assert len(fills) == 2
    assert fills[0].side == "buy"

    equity = (
        (await session.execute(select(EquitySnapshotRow).where(EquitySnapshotRow.run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(equity) == 12


async def test_two_runs_same_strategy_succeed(session: AsyncSession, tmp_path: Path) -> None:
    """Regression: StrategyRow insert must be idempotent across runs."""
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
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["buy-and-sell"]

    for _ in range(2):
        _run_id, result, _ = await run_backtest(
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
        assert result.num_fills == 2


async def test_backtest_uses_only_requested_exchange(session: AsyncSession, tmp_path: Path) -> None:
    for exchange, count in (("kraken", 12), ("binance", 6)):
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
                exchange=exchange,
            )
            for i in range(count)
        ]
        await upsert_candles(session, candles)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["buy-and-sell"]

    run_id, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
            liquidate_end=False,
            exchange="binance",
        ),
        get_sessionmaker(),
    )
    # only the 6 binance candles feed the run: the buy fills, the sell never comes
    assert result.num_fills == 1

    run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
    assert run.config["exchange"] == "binance"
