"""A missing execution period cancels limits before either engine can fill them."""

from dataclasses import replace
from datetime import timedelta

import pytest

from kaupo.core.recorder import InMemoryRecorder
from kaupo.domain import OrderIntent, OrderStatus, OrderType, RunMode, Side
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase
from tests.behaviour.test_engine import PAIR, aiter, build_engine, candle
from tests.behaviour.test_portfolio_engine import (
    BTC,
    SOL,
    steps_of,
)
from tests.behaviour.test_portfolio_engine import (
    aiter as portfolio_aiter,
)
from tests.behaviour.test_portfolio_engine import (
    build_engine as build_portfolio_engine,
)
from tests.behaviour.test_portfolio_engine import (
    candle as portfolio_candle,
)


class CopyingRecorder(InMemoryRecorder):
    async def record_order(self, order):
        # Keep each status at recording time, not aliases of a mutable order.
        await super().record_order(replace(order))


def limit(pair, price):
    return OrderIntent(pair, Side.BUY, 1, OrderType.LIMIT, limit_price=price)


@pytest.mark.parametrize("mode", [RunMode.BACKTEST, RunMode.SHADOW])
async def test_single_pair_cancels_stale_limit_then_accepts_fresh_decision(mode):
    class PostLimit(StrategyBase):
        id = "gap-limit"

        def on_candle(self, ctx):
            return [limit(PAIR, ctx.candle.close)] if ctx.position().size == 0 else []

    recorder = CopyingRecorder()
    engine = build_engine(recorder, mode=mode)
    engine.strategy = PostLimit(PostLimit.params_schema())
    first = candle(0)
    reopened = replace(first, ts=first.ts + timedelta(hours=64), open=90, high=91, low=89, close=90)
    after = replace(reopened, ts=reopened.ts + timedelta(hours=1), open=89, low=88)
    result = await engine.run(aiter([first, reopened, after]))

    assert result.num_fills == 1
    assert recorder.fills[0].ts == after.ts
    assert recorder.fills[0].price == 89
    assert float(result.final_equity) == 10_001
    original_id = recorder.orders[0].id
    assert [o.status for o in recorder.orders if o.id == original_id] == [
        OrderStatus.OPEN,
        OrderStatus.CANCELLED,
    ]
    assert all(f.order_id != original_id for f in recorder.fills)


async def test_portfolio_gap_does_not_extend_limit_while_other_pair_trades():
    class PostBoth(PortfolioStrategyBase):
        id = "portfolio-gap-limit"

        def on_candle(self, ctx):
            if ctx.candles[BTC].ts == first.ts:
                return [limit(BTC, 100), limit(SOL, 100)]
            return []

    recorder = CopyingRecorder()
    first = portfolio_candle(SOL, 0, 100)
    strategy = PostBoth(PostBoth.params_schema())
    engine = build_portfolio_engine(recorder, strategy)
    result = await engine.run(
        portfolio_aiter(
            steps_of(
                {
                    BTC: [portfolio_candle(BTC, i, 90) for i in range(66)],
                    SOL: [first, portfolio_candle(SOL, 64, 90), portfolio_candle(SOL, 65, 90)],
                }
            )
        )
    )

    assert result.num_fills == 1
    assert recorder.fills[0].pair == BTC
    assert recorder.fills[0].price == 90
    assert engine.ledger.position(SOL).size == 0
    assert [o.status for o in recorder.orders if o.pair == SOL] == [
        OrderStatus.OPEN,
        OrderStatus.CANCELLED,
    ]
    assert float(result.final_equity) == 10_000
