"""Technical indicators, pure numpy.

Wilder-smoothed indicators (RSI, ATR, ADX) use the recursive form
equivalent to ``pandas.ewm(alpha=1/n, adjust=False)`` — seeded with the
first value rather than the canonical SMA-of-first-`period` seed, so early
values differ slightly from ta-lib/TradingView (converges within ~3*n).

Warmup: ``sma``/``rolling_std``/``rolling_max``/``rolling_min``/``bollinger_bands``
yield NaN before ``period`` values exist; ``ema``/``rsi``/``atr``/``adx`` emit
values from the start (smoothed series have no NaN warmup — check your own
minimum history).
"""

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from kaupo.domain import Candle

F64 = npt.NDArray[np.float64]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def _arr(values: list[float] | F64) -> F64:
    return np.asarray(values, dtype=np.float64)


def closes(candles: Sequence[Candle]) -> F64:
    return np.array([c.close for c in candles], dtype=np.float64)


def highs(candles: Sequence[Candle]) -> F64:
    return np.array([c.high for c in candles], dtype=np.float64)


def lows(candles: Sequence[Candle]) -> F64:
    return np.array([c.low for c in candles], dtype=np.float64)


def sma(values: list[float] | F64, period: int) -> F64:
    _check_period(period)
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    if len(v) < period:
        return out
    cumsum = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1 :] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def ema(values: list[float] | F64, period: int) -> F64:
    _check_period(period)
    v = _arr(values)
    if len(v) == 0:
        return np.array([], dtype=np.float64)
    alpha = 2.0 / (period + 1)
    out = np.empty(v.shape)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rolling_std(values: list[float] | F64, period: int, ddof: int = 1) -> F64:
    _check_period(period)
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    for i in range(period - 1, len(v)):
        out[i] = np.std(v[i - period + 1 : i + 1], ddof=ddof)
    return out


def bollinger_bands(
    values: list[float] | F64, period: int = 20, num_std: float = 2.0
) -> tuple[F64, F64, F64]:
    """(middle, upper, lower)."""
    mid = sma(values, period)
    std = rolling_std(values, period)
    return mid, mid + num_std * std, mid - num_std * std


def _wilder(values: F64, period: int) -> F64:
    """Wilder smoothing; first value seeded at index 0."""
    if len(values) == 0:
        return np.array([], dtype=np.float64)
    alpha = 1.0 / period
    out = np.empty(values.shape)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(closes_: list[float] | F64, period: int = 14) -> F64:
    _check_period(period)
    c = _arr(closes_)
    out = np.full(c.shape, np.nan)
    if len(c) < 2:
        return out
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        # no losses at all -> RSI 100; no gains AND no losses (flat) -> undefined
        rs = np.where(
            avg_loss == 0,
            np.where(avg_gain == 0, np.nan, np.inf),
            avg_gain / avg_loss,
        )
    out[1:] = 100.0 - 100.0 / (1.0 + rs)
    return out


def true_range(highs_: list[float] | F64, lows_: list[float] | F64, closes_: list[float] | F64) -> F64:
    h, low, c = _arr(highs_), _arr(lows_), _arr(closes_)
    if len(h) == 0:
        return np.array([], dtype=np.float64)
    tr = np.empty(h.shape)
    tr[0] = h[0] - low[0]
    for i in range(1, len(h)):
        tr[i] = max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
    return tr


def atr(
    highs_: list[float] | F64, lows_: list[float] | F64, closes_: list[float] | F64, period: int = 14
) -> F64:
    return _wilder(true_range(highs_, lows_, closes_), period)


def adx(
    highs_: list[float] | F64,
    lows_: list[float] | F64,
    closes_: list[float] | F64,
    period: int = 14,
) -> tuple[F64, F64, F64]:
    """(adx, +DI, -DI)."""
    h, low, c = _arr(highs_), _arr(lows_), _arr(closes_)
    n = len(h)
    out_nan = np.full(h.shape, np.nan)
    if n < 2:
        return out_nan, out_nan.copy(), out_nan.copy()

    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        down = low[i - 1] - low[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    tr_smooth = _wilder(true_range(h, low, c), period)
    plus_smooth = _wilder(plus_dm, period)
    minus_smooth = _wilder(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.where(tr_smooth == 0, 0.0, plus_smooth / tr_smooth)
        minus_di = 100.0 * np.where(tr_smooth == 0, 0.0, minus_smooth / tr_smooth)
        di_sum = plus_di + minus_di
        dx = 100.0 * np.where(di_sum == 0, 0.0, np.abs(plus_di - minus_di) / di_sum)

    adx_values = _wilder(dx, period)
    adx_values[0] = np.nan  # DX at 0 is meaningless
    return adx_values, plus_di, minus_di


def rolling_max(values: list[float] | F64, period: int) -> F64:
    _check_period(period)
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    for i in range(period - 1, len(v)):
        out[i] = np.max(v[i - period + 1 : i + 1])
    return out


def rolling_min(values: list[float] | F64, period: int) -> F64:
    _check_period(period)
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    for i in range(period - 1, len(v)):
        out[i] = np.min(v[i - period + 1 : i + 1])
    return out
