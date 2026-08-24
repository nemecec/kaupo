"""Example open-source strategy: regime-switching mean-reversion <-> momentum.

Regime detection (from the design discussion):
- ADX measures trend strength
- Bollinger Band width percentile measures volatility expansion/contraction
- moving-average slope measures directional drift

RANGING market  -> mean reversion: buy when the z-score of price vs the moving
                   mean is deeply negative; exit back at the mean or on
                   overbought RSI.
TRENDING market -> momentum: buy breakouts above the N-candle high with +DI
                   leading, exit on a trailing stop.
UNCERTAIN       -> no new entries.

Design notes:
- The regime classifier deliberately has no temporal hysteresis (the design
  discussion covers why one may want it): it can flip candle to candle,
  which mainly affects entries, as exits are managed per position.
- Entry/exit bookkeeping is reconciled from the *actual* position each
  candle (not from emitted intents), so risk-manager rejections cannot
  desync the strategy's state.

This is a reference implementation for the SDK, not investment advice.
"""

import numpy as np
from pydantic import BaseModel, Field

from kaupo.domain import OrderIntent, Side
from kaupo.sdk import indicators as ind
from kaupo.sdk.protocol import StrategyBase, StrategyContext


class Params(BaseModel):
    adx_period: int = Field(default=14, gt=0)
    adx_threshold: float = 25.0
    bb_period: int = Field(default=20, gt=0)
    bb_num_std: float = 2.0
    rsi_period: int = Field(default=14, gt=0)
    rsi_overbought: float = 70.0
    entry_z_score: float = 1.5
    breakout_period: int = Field(default=20, gt=0)
    ma_period: int = Field(default=50, gt=0)
    position_fraction: float = Field(default=0.25, gt=0, le=1)
    stop_loss_pct: float = Field(default=0.03, gt=0, lt=1)
    trailing_stop_pct: float = Field(default=0.04, gt=0, lt=1)


class RegimeSwitch(StrategyBase):
    id = "regime-switch"
    params_schema = Params

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self.p: Params = params  # type: ignore[assignment]
        self._highest_since_entry: float | None = None
        self._entry_regime: str | None = None

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        p = self.p
        need = max(p.bb_period, p.breakout_period + 1, p.ma_period + 6, 2 * p.adx_period) + 5
        hist = ctx.history(need)
        if len(hist) < need:
            return []

        # reconcile bookkeeping with the ACTUAL position first: an exit that
        # got risk-rejected must not desync us; a filled exit resets state
        position = ctx.position()
        if position.size == 0 and self._entry_regime is not None:
            self._entry_regime = None
            self._highest_since_entry = None

        closes = ind.closes(hist)
        highs = ind.highs(hist)
        lows = ind.lows(hist)
        adx_v, plus_di, minus_di = ind.adx(highs, lows, closes, p.adx_period)
        mid, upper, lower = ind.bollinger_bands(closes, p.bb_period, p.bb_num_std)
        rsi_v = ind.rsi(closes, p.rsi_period)
        ma = ind.sma(closes, p.ma_period)
        std = ind.rolling_std(closes, p.bb_period)

        close = closes[-1]
        if (
            np.isnan(adx_v[-1])
            or np.isnan(mid[-1])
            or np.isnan(rsi_v[-1])
            or np.isnan(ma[-6])
            or np.isnan(std[-1])
        ):
            return []

        regime = self._regime(adx_v[-1], upper, lower, mid, ma, close)

        if position.size > 0:
            return self._exits(ctx, position.size, close, mid[-1], rsi_v[-1], regime)
        z_score = float((close - mid[-1]) / std[-1]) if std[-1] > 0 else 0.0
        return self._entries(ctx, close, highs, z_score, plus_di, minus_di, regime)

    # -- internals ---------------------------------------------------------

    def _regime(
        self,
        adx_now: float,
        upper: np.ndarray,
        lower: np.ndarray,
        mid: np.ndarray,
        ma: np.ndarray,
        close: float,
    ) -> str:
        p = self.p
        score = 0

        if adx_now > p.adx_threshold:
            score += 2
        elif adx_now < p.adx_threshold - 5:
            score -= 2

        widths = (upper - lower) / mid
        valid = widths[~np.isnan(widths)]
        percentile = float((valid < widths[-1]).mean()) if len(valid) else 0.5
        score += 1 if percentile > 0.5 else -1

        slope = ma[-1] - ma[-6]
        if abs(slope) > close * 0.01:
            score += 1

        if score >= 2:
            return "trending"
        if score <= -1:
            return "ranging"
        return "uncertain"

    def _entries(
        self,
        ctx: StrategyContext,
        close: float,
        highs: np.ndarray,
        z_score: float,
        plus_di: np.ndarray,
        minus_di: np.ndarray,
        regime: str,
    ) -> list[OrderIntent]:
        p = self.p
        pair = ctx.candle.pair
        size = (ctx.equity() * p.position_fraction) / close
        stop = close * (1 - p.stop_loss_pct)

        if regime == "ranging" and z_score <= -p.entry_z_score:
            self._entry_regime = "ranging"
            self._highest_since_entry = close
            return [OrderIntent(pair=pair, side=Side.BUY, size=size, stop_loss=stop, reason="mr entry")]

        if regime == "trending":
            prev_high = float(np.max(highs[-p.breakout_period - 1 : -1]))
            if close > prev_high and plus_di[-1] > minus_di[-1]:
                self._entry_regime = "trending"
                self._highest_since_entry = close
                return [
                    OrderIntent(pair=pair, side=Side.BUY, size=size, stop_loss=stop, reason="momentum entry")
                ]
        return []

    def _exits(
        self,
        ctx: StrategyContext,
        size: float,
        close: float,
        mid_band: float,
        rsi_now: float,
        regime: str,
    ) -> list[OrderIntent]:
        p = self.p
        pair = ctx.candle.pair
        self._highest_since_entry = max(self._highest_since_entry or close, close)

        reason = ""
        if self._entry_regime == "ranging":
            if close >= mid_band:
                reason = "mr exit: mean reached"
            elif rsi_now >= p.rsi_overbought:
                reason = "mr exit: overbought"
        else:  # trending entry (or unknown)
            if close < self._highest_since_entry * (1 - p.trailing_stop_pct):
                reason = "momentum exit: trailing stop"
            elif regime == "ranging":
                reason = "momentum exit: regime lost"

        if reason:
            # state is reset in on_candle once the position is actually flat
            return [OrderIntent(pair=pair, side=Side.SELL, size=size, reason=reason)]
        return []
