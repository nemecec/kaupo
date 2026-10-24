"""Momentum rotation keeps its step cadence across missing candle periods."""

from datetime import UTC, datetime, timedelta

import pytest

from examples.strategies.momentum_rotation import MomentumRotation, Params
from kaupo.domain import Candle, Pair, Timeframe

BTC = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class Context:
    def __init__(self, timeframe, gap):
        self.bars = [
            Candle(
                pair=BTC,
                timeframe=timeframe,
                ts=BASE + timedelta(seconds=offset),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1,
            )
            for offset, price in [(0, 100), (gap, 110)]
        ]
        self.candles = {BTC: self.bars[-1]}

    def history(self, pair, n):
        return self.bars[-n:]

    def positions(self):
        return {}

    def cash(self):
        return 1000

    def equity(self):
        return 1000


@pytest.mark.parametrize("timeframe,steps", [(Timeframe.H1, 168), (Timeframe.H4, 42), (Timeframe.D1, 7)])
@pytest.mark.parametrize("gap", [3600, 64 * 3600, 7 * 86400])
def test_default_cadence_ignores_elapsed_gap(timeframe, steps, gap):
    strategy = MomentumRotation(Params(lookback=1))
    ctx = Context(timeframe, gap)
    # Positive momentum makes the first completed rebalance buy observable.
    # Phase one sells nothing from an all-cash portfolio; phase two buys.
    for _ in range(steps):
        assert strategy.on_candle(ctx) == []
    (buy,) = strategy.on_candle(ctx)
    assert buy.pair == BTC
    assert buy.reason == "rebalance entry"


@pytest.mark.parametrize("gap", [3600, 64 * 3600])
def test_explicit_interval_preserves_two_phase_rebalance(gap):
    strategy = MomentumRotation(Params(lookback=1, rebalance_interval=3))
    ctx = Context(Timeframe.H1, gap)
    for _ in range(3):
        assert strategy.on_candle(ctx) == []
    assert len(strategy.on_candle(ctx)) == 1


def test_single_bar_does_not_trigger_early_target_selection():
    strategy = MomentumRotation(Params(lookback=1))
    ctx = Context(Timeframe.H1, 3600)
    ctx.bars = ctx.bars[-1:]
    calls = []
    strategy._targets = lambda ctx: calls.append(ctx) or {}
    assert strategy.on_candle(ctx) == []
    assert calls == []
