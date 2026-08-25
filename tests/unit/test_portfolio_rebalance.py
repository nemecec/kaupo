"""Rebalance helper: weights -> intents, dust threshold, two-phase cash rule."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.domain import Candle, Pair, Position, Side, Timeframe
from kaupo.sdk.portfolio import plan_rebalance

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
ADA = Pair.parse("ADA/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(pair: Pair, close: float, i: int = 0) -> Candle:
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1.0,
    )


class FakeCtx:
    """Minimal PortfolioContext stub."""

    def __init__(self, candles=None, histories=None, positions=None, cash=0.0, equity=0.0):
        self._candles = candles or {}
        self._histories = histories or {}
        self._positions = positions or {}
        self._cash = cash
        self._equity = equity

    @property
    def clock(self):
        raise NotImplementedError

    @property
    def candles(self):
        return self._candles

    def history(self, pair, n):
        hist = self._histories.get(pair, [])
        return hist[-n:] if n <= len(hist) else hist

    def positions(self):
        return self._positions

    def cash(self):
        return self._cash

    def equity(self):
        return self._equity


def test_all_cash_buys_to_target_weights() -> None:
    ctx = FakeCtx(candles={BTC: candle(BTC, 100), SOL: candle(SOL, 50)}, cash=10_000, equity=10_000)
    plan = plan_rebalance({BTC: 0.5, SOL: 0.25}, ctx)
    assert plan.sells == []
    assert [(b.pair, b.side) for b in plan.buys] == [(BTC, Side.BUY), (SOL, Side.BUY)]
    assert plan.buys[0].size == pytest.approx(50.0)  # 5000 / 100
    assert plan.buys[1].size == pytest.approx(50.0)  # 2500 / 50
    assert all(b.reason == "rebalance entry" for b in plan.buys)


def test_dust_churn_below_min_trade_value_is_skipped() -> None:
    # position value 100, target 105: the 5 EUR diff is dust
    ctx = FakeCtx(
        candles={BTC: candle(BTC, 100)},
        positions={BTC: Position(pair=BTC, size=1.0, avg_entry=100)},
        cash=9_900,
        equity=10_000,
    )
    plan = plan_rebalance({BTC: 0.0105}, ctx, min_trade_value=10.0)
    assert plan.sells == []
    assert plan.buys == []


def test_full_exit_always_sells_even_below_threshold() -> None:
    # position value 5 (dust): a zero target still closes it entirely
    ctx = FakeCtx(
        candles={BTC: candle(BTC, 100)},
        positions={BTC: Position(pair=BTC, size=0.05, avg_entry=100)},
        cash=9_995,
        equity=10_000,
    )
    plan = plan_rebalance({}, ctx, min_trade_value=10.0)
    (sell,) = plan.sells
    assert sell.pair == BTC and sell.side is Side.SELL
    assert sell.size == pytest.approx(0.05)
    assert sell.reason == "rebalance exit"
    assert plan.buys == []


def test_buys_never_spend_same_plan_sell_proceeds() -> None:
    # rotate fully: 5000 in BTC, 1000 free cash, target all-SOL.
    # The plan sells BTC AND buys SOL — but the buy is capped by the 1000 of
    # free cash, not by the 5000 the sell will raise. That is the two-phase
    # rule: emit sells now, replan and buy next step.
    ctx = FakeCtx(
        candles={BTC: candle(BTC, 100), SOL: candle(SOL, 50)},
        positions={BTC: Position(pair=BTC, size=50.0, avg_entry=100)},
        cash=1_000,
        equity=6_000,
    )
    plan = plan_rebalance({SOL: 1.0}, ctx)
    (sell,) = plan.sells
    assert sell.pair == BTC and sell.size == pytest.approx(50.0)
    (buy,) = plan.buys
    assert buy.pair == SOL
    assert buy.size * 50 == pytest.approx(1_000.0)  # free cash only


def test_cash_allocated_in_sorted_pair_order() -> None:
    ctx = FakeCtx(
        candles={ADA: candle(ADA, 10), SOL: candle(SOL, 10)},
        cash=3_000,
        equity=10_000,
    )
    plan = plan_rebalance({SOL: 0.5, ADA: 0.5}, ctx)  # 5000 each, only 3000 free
    (buy,) = plan.buys
    assert buy.pair == ADA  # sorted first, takes the whole constrained budget
    assert buy.size == pytest.approx(300.0)


def test_pair_without_price_data_is_left_alone() -> None:
    ctx = FakeCtx(candles={}, histories={}, cash=10_000, equity=10_000)
    plan = plan_rebalance({BTC: 0.5}, ctx)
    assert plan.buys == []

    # history provides the last known close when there is no candle this step
    ctx = FakeCtx(histories={BTC: [candle(BTC, 100)]}, cash=10_000, equity=10_000)
    plan = plan_rebalance({BTC: 0.5}, ctx)
    assert len(plan.buys) == 1


def test_invalid_weights_rejected() -> None:
    ctx = FakeCtx(candles={BTC: candle(BTC, 100)}, cash=10_000, equity=10_000)
    with pytest.raises(ValueError, match="sum"):
        plan_rebalance({BTC: 0.6, SOL: 0.6}, ctx)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        plan_rebalance({BTC: -0.1}, ctx)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        plan_rebalance({BTC: 1.1}, ctx)


def test_reduce_sell_is_partial_and_reasoned() -> None:
    # position value 5000, target 2000: sell 3000 worth
    ctx = FakeCtx(
        candles={BTC: candle(BTC, 100)},
        positions={BTC: Position(pair=BTC, size=50.0, avg_entry=100)},
        cash=5_000,
        equity=10_000,
    )
    plan = plan_rebalance({BTC: 0.2}, ctx)
    (sell,) = plan.sells
    assert sell.size == pytest.approx(30.0)
    assert sell.reason == "rebalance reduce"
    assert plan.buys == []
