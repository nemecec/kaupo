"""Timestamp-join of per-pair candle streams (portfolio engine input)."""

from datetime import UTC, datetime, timedelta

from kaupo.core.portfolio_engine import joined_steps
from kaupo.domain import Candle, Pair, Timeframe

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
ADA = Pair.parse("ADA/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(pair: Pair, i: int) -> Candle:
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=100 + i,
        high=101 + i,
        low=99 + i,
        close=100 + i,
        volume=1.0,
    )


def test_aligned_pairs_join_one_step_per_timestamp() -> None:
    btc = [candle(BTC, i) for i in range(3)]
    sol = [candle(SOL, i) for i in range(3)]
    steps = list(joined_steps({BTC: btc, SOL: sol}))
    assert len(steps) == 3
    for i, (ts, step) in enumerate(steps):
        assert ts == BASE + timedelta(hours=i)
        assert set(step) == {BTC, SOL}


def test_pairs_with_missing_candles_skip_the_step() -> None:
    # SOL has no candle at hour 1; BTC has no candle at hour 2
    sol = [candle(SOL, 0), candle(SOL, 2)]
    btc = [candle(BTC, 0), candle(BTC, 1)]
    steps = list(joined_steps({BTC: btc, SOL: sol}))
    assert [set(step) for _, step in steps] == [{BTC, SOL}, {BTC}, {SOL}]
    assert [ts for ts, _ in steps] == [BASE, BASE + timedelta(hours=1), BASE + timedelta(hours=2)]


def test_step_pairs_are_in_sorted_pair_string_order() -> None:
    # insertion order shuffled: iteration must still be sorted (ADA < BTC < SOL)
    steps = list(
        joined_steps(
            {
                SOL: [candle(SOL, 0)],
                ADA: [candle(ADA, 0)],
                BTC: [candle(BTC, 0)],
            }
        )
    )
    (only,) = steps
    assert list(only[1]) == [ADA, BTC, SOL]


def test_different_lengths_and_empty_input() -> None:
    steps = list(joined_steps({BTC: [candle(BTC, i) for i in range(3)], SOL: [candle(SOL, 0)]}))
    assert [set(step) for _, step in steps] == [{BTC, SOL}, {BTC}, {BTC}]
    assert list(joined_steps({BTC: [], SOL: []})) == []
    assert list(joined_steps({})) == []
