import numpy as np
import pandas as pd
import pytest

from kaupo.sdk import indicators as ind


@pytest.fixture
def series() -> np.ndarray:  # type: ignore[type-arg]
    rng = np.random.default_rng(42)
    return 100 + np.cumsum(rng.normal(0, 1, 200))


def pd_sma(v: pd.Series, n: int) -> pd.Series:
    return v.rolling(n).mean()


def test_sma(series: np.ndarray) -> None:  # type: ignore[type-arg]
    expected = pd_sma(pd.Series(series), 10).to_numpy()
    np.testing.assert_allclose(ind.sma(series, 10), expected, equal_nan=True)


def test_ema(series: np.ndarray) -> None:  # type: ignore[type-arg]
    expected = pd.Series(series).ewm(span=10, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(ind.ema(series, 10), expected, rtol=1e-10)


def test_rolling_std(series: np.ndarray) -> None:  # type: ignore[type-arg]
    expected = pd.Series(series).rolling(20).std().to_numpy()  # ddof=1 default
    np.testing.assert_allclose(ind.rolling_std(series, 20), expected, rtol=1e-10)


def test_bollinger(series: np.ndarray) -> None:  # type: ignore[type-arg]
    mid, upper, lower = ind.bollinger_bands(series, 20, 2.0)
    s = pd.Series(series)
    np.testing.assert_allclose(mid, s.rolling(20).mean().to_numpy(), equal_nan=True)
    np.testing.assert_allclose(upper - mid, 2 * s.rolling(20).std().to_numpy(), equal_nan=True)
    np.testing.assert_allclose(mid - lower, 2 * s.rolling(20).std().to_numpy(), equal_nan=True)


def wilder(v: pd.Series, n: int) -> pd.Series:
    return v.ewm(alpha=1 / n, adjust=False).mean()


def test_rsi(series: np.ndarray) -> None:  # type: ignore[type-arg]
    s = pd.Series(series)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = wilder(gain, 14) / wilder(loss, 14)
    expected = (100 - 100 / (1 + rs)).to_numpy()

    result = ind.rsi(series, 14)
    assert np.isnan(result[0])
    np.testing.assert_allclose(result[1:], expected[1:], rtol=1e-10)


def test_atr() -> None:
    h = np.array([10, 12, 11, 13, 12, 14, 13, 15, 14, 16], dtype=float)
    low = np.array([8, 9, 9, 10, 10, 11, 11, 12, 12, 13], dtype=float)
    c = np.array([9, 11, 10, 12, 11, 13, 12, 14, 13, 15], dtype=float)

    tr = pd.Series(
        [h[0] - low[0]]
        + [max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])) for i in range(1, len(h))]
    )
    expected = wilder(tr, 3).to_numpy()
    np.testing.assert_allclose(ind.atr(h, low, c, 3), expected, rtol=1e-10)


def test_adx_trending_up_is_high() -> None:
    # steadily rising market -> strong trend
    n = 100
    base = np.linspace(100, 200, n)
    h = base + 1
    low = base - 1
    c = base
    adx_values, plus_di, minus_di = ind.adx(h, low, c, 14)
    assert adx_values[-1] > 40
    assert plus_di[-1] > minus_di[-1]


def test_adx_choppy_is_low() -> None:
    # alternating up/down -> no trend
    n = 100
    base = 100 + np.array([(-1) ** i for i in range(n)], dtype=float)
    h = base + 1
    low = base - 1
    c = base
    adx_values, _, _ = ind.adx(h, low, c, 14)
    assert adx_values[-1] < 20


def test_rolling_max_min(series: np.ndarray) -> None:  # type: ignore[type-arg]
    s = pd.Series(series)
    np.testing.assert_allclose(ind.rolling_max(series, 5), s.rolling(5).max().to_numpy(), equal_nan=True)
    np.testing.assert_allclose(ind.rolling_min(series, 5), s.rolling(5).min().to_numpy(), equal_nan=True)


def test_candle_extractors() -> None:
    from datetime import UTC, datetime

    from kaupo.domain import Candle, Pair, Timeframe

    candles = [
        Candle(
            pair=Pair.parse("BTC/EUR"),
            timeframe=Timeframe.H1,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
        )
    ]
    np.testing.assert_array_equal(ind.closes(candles), [1.5])
    np.testing.assert_array_equal(ind.highs(candles), [2.0])
    np.testing.assert_array_equal(ind.lows(candles), [0.5])


def test_short_series_warmup() -> None:
    out = ind.sma([1.0, 2.0], 5)
    assert np.all(np.isnan(out))
    out = ind.rsi([1.0], 14)
    assert np.all(np.isnan(out))
