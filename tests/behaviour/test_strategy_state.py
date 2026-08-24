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

    # ranging entry state with an open position
    strategy._entry_regime = "ranging"
    strategy._highest_since_entry = 100.0

    # choppy market whose final candle closes above the moving average:
    # the "mr exit: mean reached" exit MUST fire on this data
    prices = [100 + 2 * ((i % 8) / 8 - 0.5) for i in range(79)] + [101.5]
    candles = [candle(i, p) for i, p in enumerate(prices)]

    first = strategy.on_candle(FakeCtx(candles, position_size=1.0))
    assert first, "expected an exit intent on this data"
    assert first[0].side is Side.SELL
    assert first[0].reason.startswith("mr exit")

    # simulate risk rejection: position STILL open next candle — the strategy
    # must keep managing the ranging exit, not fall into the momentum branch
    second = strategy.on_candle(FakeCtx(candles, position_size=1.0))
    assert strategy._entry_regime == "ranging"
    assert second, "expected the exit to be re-proposed"
    assert second[0].reason.startswith("mr exit")


def test_state_resets_once_position_is_flat() -> None:
    strategy = load_strategies(STRATEGIES_DIR)["regime-switch"].create({})
    strategy._entry_regime = "ranging"
    strategy._highest_since_entry = 100.0

    candles = [candle(i, 100.0) for i in range(80)]
    strategy.on_candle(FakeCtx(candles, position_size=0.0))
    assert strategy._entry_regime is None
    assert strategy._highest_since_entry is None
