"""End-of-run fills must persist even when every recorder call flushes."""

from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.core.recorder import DbRecorder
from kaupo.data.candles import upsert_candles
from kaupo.db.models import FillRow, OrderRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("portfolio", [False, True], ids=["single-pair", "portfolio"])
async def test_liquidation_persists_across_flush_boundary(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portfolio: bool
) -> None:
    # Every call commits independently, so a fill cannot rely on a later
    # record_order call sharing its batch (the intermittent failure in #53).
    module = "portfolio" if portfolio else "run"
    monkeypatch.setattr(f"kaupo.backtest.{module}.DbRecorder", partial(DbRecorder, flush_every=1))
    pairs = [Pair.parse("BTC/EUR"), Pair.parse("SOL/EUR")] if portfolio else [Pair.parse("BTC/EUR")]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for pair in pairs:
        await upsert_candles(
            session,
            [
                Candle(
                    pair=pair,
                    timeframe=Timeframe.H1,
                    ts=start + timedelta(hours=i),
                    open=100 + i,
                    high=102 + i,
                    low=99 + i,
                    close=101 + i,
                    volume=10,
                )
                for i in range(4)
            ],
        )
    await session.commit()
    base = "PortfolioStrategyBase" if portfolio else "StrategyBase"
    universe = "ctx.candles" if portfolio else "[ctx.candle.pair]"
    (tmp_path / "hold.py").write_text(
        f"from kaupo.sdk.protocol import {base}\n"
        "from kaupo.domain import OrderIntent, Side\n"
        f"class Hold({base}):\n"
        "    id = 'hold'\n"
        "    def __init__(self, params):\n"
        "        super().__init__(params)\n"
        "        self.entered = False\n"
        "    def on_candle(self, ctx):\n"
        "        if self.entered:\n"
        "            return []\n"
        "        self.entered = True\n"
        f"        return [OrderIntent(pair=p, side=Side.BUY, size=1) for p in {universe}]\n"
    )
    strategy = load_strategies(tmp_path)["hold"]
    if portfolio:
        run_id, result, _ = await run_portfolio_backtest(
            PortfolioBacktestRequest(
                strategy=strategy,
                params={},
                pairs=pairs,
                timeframe=Timeframe.H1,
                start=start,
                end=start + timedelta(hours=4),
                liquidate_end=True,
            ),
            get_sessionmaker(),
        )
    else:
        run_id, result, _ = await run_backtest(
            BacktestRequest(
                strategy=strategy,
                params={},
                pair=pairs[0],
                timeframe=Timeframe.H1,
                start=start,
                end=start + timedelta(hours=4),
                liquidate_end=True,
            ),
            get_sessionmaker(),
        )
    assert result.status.value == "completed"
    run = await session.get(RunRow, run_id)
    assert run is not None and run.status == "completed"
    orders = (await session.scalars(select(OrderRow).where(OrderRow.run_id == run_id))).all()
    fills = (await session.scalars(select(FillRow).where(FillRow.run_id == run_id))).all()
    assert len(orders) == len(fills) == 2 * len(pairs)
    assert all(order.status == "filled" for order in orders)
    assert {fill.order_id for fill in fills} == {order.id for order in orders}
    exits = [order for order in orders if order.reason == "end-of-run liquidation"]
    assert {order.pair for order in exits} == {str(pair) for pair in pairs}
    assert all(order.side == "sell" for order in exits)
