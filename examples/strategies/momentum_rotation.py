"""Example portfolio strategy: equal-weight top-K momentum rotation.

Ranks the universe by total return over ``lookback`` candles. Holds the top
``top_k`` pairs with a positive return, at equal weight, long-only. Pairs
with a non-positive return stay in cash. Rebalances every
``rebalance_interval`` steps (default: one week, derived from the observed
candle spacing).

Each rebalance runs in two phases (see kaupo.sdk.portfolio): the rebalance
step emits the exits and reductions; the next step emits the entries and
adds, sized from free cash. Buys never depend on same-step sell proceeds.

``cash_buffer_pct`` keeps a fraction of equity uninvested.

This is a reference implementation for the portfolio SDK, not investment
advice.
"""

from pydantic import BaseModel, Field

from kaupo.domain import OrderIntent, Pair
from kaupo.sdk.portfolio import plan_rebalance
from kaupo.sdk.protocol import PortfolioContext, PortfolioStrategyBase


class Params(BaseModel):
    lookback: int = Field(default=24 * 30, gt=0)  # candles of return history
    top_k: int = Field(default=3, gt=0)
    rebalance_interval: int | None = Field(default=None, gt=0)  # steps; null = one week
    cash_buffer_pct: float = Field(default=0.0, ge=0, lt=1)
    min_trade_value: float = Field(default=10.0, gt=0)  # quote; below this, trades are dust


class MomentumRotation(PortfolioStrategyBase):
    id = "momentum-rotation"
    params_schema = Params

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self.p: Params = params  # type: ignore[assignment]
        self._seen: set[Pair] = set()  # universe pairs observed so far
        self._step = 0
        self._pending_targets: dict[Pair, float] | None = None

    def on_candle(self, ctx: PortfolioContext) -> list[OrderIntent]:
        p = self.p
        self._seen.update(ctx.candles)
        self._step += 1

        # phase 2 of the last rebalance: entries/adds into the stored targets
        if self._pending_targets is not None:
            targets, self._pending_targets = self._pending_targets, None
            return plan_rebalance(targets, ctx, min_trade_value=p.min_trade_value).buys

        interval = p.rebalance_interval or self._week_in_steps(ctx)
        if self._step % interval != 0:
            return []

        targets = self._targets(ctx)
        if not targets:
            return []
        plan = plan_rebalance(targets, ctx, min_trade_value=p.min_trade_value)
        self._pending_targets = targets
        return plan.sells

    # -- internals ---------------------------------------------------------

    def _targets(self, ctx: PortfolioContext) -> dict[Pair, float]:
        """Equal weights for the top-K pairs by lookback return (positive only)."""
        p = self.p
        returns: list[tuple[float, Pair]] = []
        for pair in sorted(self._seen, key=str):
            hist = ctx.history(pair, p.lookback + 1)
            if len(hist) < p.lookback + 1:
                continue  # not enough history for this pair yet
            base = hist[0].close
            if base <= 0:
                continue
            returns.append((hist[-1].close / base - 1.0, pair))
        returns.sort(key=lambda item: (-item[0], str(item[1])))  # deterministic tie-break
        winners = [pair for ret, pair in returns[: p.top_k] if ret > 0]
        if not winners:
            return {}
        weight = (1.0 - p.cash_buffer_pct) / len(winners)
        return {pair: weight for pair in winners}

    def _week_in_steps(self, ctx: PortfolioContext) -> int:
        """One week in steps, derived from the observed candle spacing."""
        for pair in sorted(self._seen, key=str):
            hist = ctx.history(pair, 2)
            if len(hist) == 2:
                seconds = (hist[-1].ts - hist[-2].ts).total_seconds()
                if seconds > 0:
                    return max(1, round(7 * 86400 / seconds))
        return 1
