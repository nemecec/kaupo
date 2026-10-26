"""Splitting a candle sequence across restarts must preserve the risk decision."""

from dataclasses import replace

import pytest

from kaupo.core.recorder import InMemoryRecorder
from kaupo.domain import OrderIntent, RunMode, RunStatus, Side
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskManager
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase
from tests.behaviour import test_engine as single
from tests.behaviour import test_portfolio_engine as portfolio
from tests.behaviour.test_gap_risk import FRIDAY, candle, risk


@pytest.mark.parametrize("portfolio_mode", [False, True])
async def test_restarted_engine_retains_weekend_loss_baseline(portfolio_mode):
    pair = portfolio.BTC if portfolio_mode else single.PAIR

    class BuyFriday(PortfolioStrategyBase if portfolio_mode else StrategyBase):
        id = "buy-friday"

        def on_candle(self, ctx):
            ts = ctx.candles[pair].ts if portfolio_mode else ctx.candle.ts
            return [OrderIntent(pair, Side.BUY, 10)] if ts == FRIDAY else []

    def build(recorder, manager, ledger=None):
        strategy = BuyFriday(BuyFriday.params_schema())
        if portfolio_mode:
            engine = portfolio.build_engine(recorder, strategy, pairs=(pair,), liquidate_end=False)
            engine.risk = manager
        else:
            engine = single.build_engine(recorder, risk=manager, mode=RunMode.SHADOW)
            engine.strategy = strategy
            engine.config = replace(engine.config, liquidate_end=False)
        engine.run_info = replace(engine.run_info, mode=RunMode.SHADOW)
        if ledger is not None:
            engine.ledger = ledger
        return engine

    async def stream(points):
        for point in points:
            yield (point.ts, {pair: point}) if portfolio_mode else point

    points = [candle(pair, h, p) for h, p in [(0, 100), (1, 100), (65, 70)]]
    first_recorder = InMemoryRecorder()
    first = build(first_recorder, risk())
    await first.run(stream(points[:2]))
    assert first_recorder.risk_state is not None
    second_recorder = InMemoryRecorder()
    second = build(
        second_recorder,
        RiskManager.restored(first.risk.config, first_recorder.risk_state),
        Ledger("EUR", first.ledger.cash, points[1].ts, positions=first.ledger.open_positions),
    )
    result = await second.run(stream(points[2:]))
    uninterrupted = build(InMemoryRecorder(), risk())
    expected = await uninterrupted.run(stream(points))
    assert result.status is expected.status is RunStatus.HALTED
    assert result.halt_reason == expected.halt_reason
    assert second_recorder.risk_state == uninterrupted.risk.checkpoint()


async def test_failed_candle_keeps_incomplete_checkpoint():
    class Broken(StrategyBase):
        id = "broken"

        def on_candle(self, ctx):
            raise RuntimeError("strategy failed")

    recorder = InMemoryRecorder()
    engine = single.build_engine(recorder, mode=RunMode.SHADOW)
    engine.strategy = Broken(Broken.params_schema())
    with pytest.raises(RuntimeError, match="strategy failed"):
        await engine.run(single.aiter([single.candle(0)]))
    assert recorder.equity  # a snapshot alone must not certify a completed candle
    assert recorder.risk_state == {"version": 1, "incomplete": True}
