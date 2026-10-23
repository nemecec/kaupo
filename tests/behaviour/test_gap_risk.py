"""Losses across a closure halt both engines before the next strategy decision."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kaupo.core.recorder import InMemoryRecorder
from kaupo.domain import OrderIntent, RunMode, RunStatus, Side
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase
from tests.behaviour import test_engine as single
from tests.behaviour import test_portfolio_engine as portfolio

FRIDAY = datetime(2026, 1, 2, 20, tzinfo=UTC)


def risk():
    return RiskManager(
        RiskConfig(
            max_position_quote=10_000,
            max_gross_exposure_quote=10_000,
            max_daily_loss_quote=200,
        )
    )


def candle(pair, hours, price):
    return replace(
        portfolio.candle(pair, 0, price),
        ts=FRIDAY + timedelta(hours=hours),
    )


@pytest.mark.parametrize("mode", [RunMode.BACKTEST, RunMode.SHADOW])
async def test_single_pair_weekend_loss_halts_before_strategy(mode):
    class BuyOnce(StrategyBase):
        id = "buy-before-gap"

        def __init__(self, params):
            super().__init__(params)
            self.calls = []

        def on_candle(self, ctx):
            self.calls.append(ctx.candle.ts)
            if len(self.calls) == 1:
                return [OrderIntent(single.PAIR, Side.BUY, 10)]
            return []

    recorder = InMemoryRecorder()
    engine = single.build_engine(recorder, risk=risk(), mode=mode)
    strategy = BuyOnce(BuyOnce.params_schema())
    engine.strategy = strategy
    candles = [candle(single.PAIR, h, p) for h, p in [(0, 100), (1, 100), (65, 70), (66, 100)]]
    result = await engine.run(single.aiter(candles))

    assert result.status is RunStatus.HALTED
    assert "9700.00 vs day start 10000.00" in result.halt_reason
    assert strategy.calls == [c.ts for c in candles[:2]]
    assert result.num_fills == 1
    assert float(result.final_equity) == 9_700
    assert recorder.equity[-1][0] == candles[2].ts
    assert recorder.final_status is RunStatus.HALTED


@pytest.mark.parametrize("other_pair_trades", [False, True])
async def test_portfolio_reopening_loss_counts(other_pair_trades):
    class BuyOnce(PortfolioStrategyBase):
        id = "portfolio-buy-before-gap"

        def __init__(self, params):
            super().__init__(params)
            self.calls = []

        def on_candle(self, ctx):
            self.calls.append(ctx.candles[portfolio.BTC].ts)
            if len(self.calls) == 1:
                return [OrderIntent(portfolio.SOL, Side.BUY, 10)]
            return []

    recorder = InMemoryRecorder()
    strategy = BuyOnce(BuyOnce.params_schema())
    engine = portfolio.build_engine(recorder, strategy, risk=risk())
    other_hours = range(67) if other_pair_trades else [0, 1, 65, 66]
    steps = portfolio.steps_of(
        {
            portfolio.BTC: [candle(portfolio.BTC, h, 100) for h in other_hours],
            portfolio.SOL: [
                candle(portfolio.SOL, h, p) for h, p in [(0, 100), (1, 100), (65, 70), (66, 100)]
            ],
        }
    )
    result = await engine.run(portfolio.aiter(steps))

    assert result.status is RunStatus.HALTED
    assert "9700.00 vs day start 10000.00" in result.halt_reason
    assert len(strategy.calls) == (65 if other_pair_trades else 2)
    assert strategy.calls[-1] == FRIDAY + timedelta(hours=64 if other_pair_trades else 1)
    assert result.num_fills == 1
    assert float(result.final_equity) == 9_700
    assert recorder.equity[-1][0] == FRIDAY + timedelta(hours=65)
    assert recorder.final_status is RunStatus.HALTED
