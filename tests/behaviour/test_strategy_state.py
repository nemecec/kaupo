"""Regression: a risk-rejected exit must not desync the strategy's state."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kaupo.domain import Candle, Pair, Position, Side, Timeframe
from kaupo.sdk.loader import load_strategies

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"


class FakeClock:
    def __init__(self, ts: datetime) -> None:
        self._ts = ts

    def now(self) -> datetime:
        return self._ts


class FakeCtx:
    def __init__(self, candles: list[Candle], position_size: float) -> None:
        self._candles = candles
        self._position_size = position_size
        self.clock = FakeClock(candles[-1].ts)

    @property
    def candle(self) -> Candle:
        return self._candles[-1]

    def history(self, n: int) -> list[Candle]:
        return self._candles[-n:] if n < len(self._candles) else self._candles

    def position(self) -> Position:
        return Position(pair=PAIR, size=self._position_size, avg_entry=100.0)

    def cash(self) -> float:
        return 5_000.0

    def equity(self) -> float:
        return 10_000.0


def candle(i: int, price: float) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=price,
        high=price * 1.005,
        low=price * 0.995,
        close=price,
        volume=1.0,
    )


def test_rejected_exit_does_not_desync_state() -> None:
    strategy = load_strategies(STRATEGIES_DIR)["regime-switch"].create({})

    # put the strategy in a ranging entry state
    strategy._entry_regime = "ranging"
    strategy._highest_since_entry = 100.0

    # choppy market where close >= mid band triggers "mr exit: mean reached"
    prices = [100 + (i % 7) - 3 for i in range(80)]
    candles = [candle(i, p) for i, p in enumerate(prices)]

    first = strategy.on_candle(FakeCtx(candles, position_size=1.0))
    if first:  # an exit was proposed
        assert first[0].side is Side.SELL
        # simulate risk rejection: position STILL open next candle
        second = strategy.on_candle(FakeCtx(candles, position_size=1.0))
        # the strategy must still know it holds a ranging entry and keep
        # managing the exit — not fall into the momentum/unknown branch
        assert strategy._entry_regime == "ranging"
        if second:
            assert second[0].reason.startswith("mr exit")
    else:
        # no exit triggered on this data; state must still be intact
        assert strategy._entry_regime == "ranging"


def test_state_resets_once_position_is_flat() -> None:
    strategy = load_strategies(STRATEGIES_DIR)["regime-switch"].create({})
    strategy._entry_regime = "ranging"
    strategy._highest_since_entry = 100.0

    candles = [candle(i, 100.0) for i in range(80)]
    strategy.on_candle(FakeCtx(candles, position_size=0.0))
    assert strategy._entry_regime is None
    assert strategy._highest_since_entry is None
