"""Rebalance helper for portfolio strategies: from target weights to intents.

``plan_rebalance`` diffs the current allocation (from the context) against
target weights (fractions of equity, each in [0, 1], summing to at most 1)
and returns a :class:`RebalancePlan` with two intent lists:

- ``sells``: reductions and exits. A pair absent from ``targets`` is fully
  exited, even below ``min_trade_value``, so no dust position lingers.
- ``buys``: entries and adds, sized from the *free cash at plan time only*.

Buys never spend the proceeds of sells from the same plan. This is the
two-phase rule: emit ``sells`` on one step and ``buys`` on the next
(recompute the plan from the same targets after the sells filled). Emitting
both lists in one step is also safe — the buys just leave the same-step sell
proceeds idle, since fills happen one step later either way.

Trades smaller than ``min_trade_value`` (in quote currency) are skipped to
avoid dust churn. All lists are in sorted pair-string order: deterministic.
"""

from dataclasses import dataclass

from kaupo.domain import OrderIntent, Pair, Position, Side
from kaupo.sdk.protocol import PortfolioContext

WEIGHT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class RebalancePlan:
    """The diff between current and target allocation, split by phase."""

    sells: list[OrderIntent]  # reductions/exits — emit these first
    buys: list[OrderIntent]  # entries/adds sized from free cash — emit next step


def _last_price(ctx: PortfolioContext, pair: Pair) -> float | None:
    """The pair's last known close: this step's candle, else its history."""
    candle = ctx.candles.get(pair)
    if candle is not None:
        return candle.close
    hist = ctx.history(pair, 1)
    return hist[-1].close if hist else None


def plan_rebalance(
    targets: dict[Pair, float],
    ctx: PortfolioContext,
    *,
    min_trade_value: float = 10.0,
) -> RebalancePlan:
    """Compute the intents that move the current allocation to ``targets``.

    Weights are fractions of the current equity. Buys are allocated in
    sorted pair order from the free cash; when cash runs out, later buys
    shrink below ``min_trade_value`` and drop out.
    """
    for pair, weight in targets.items():
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"target weight for {pair} must be in [0, 1], got {weight}")
    total = sum(targets.values())
    if total > 1.0 + WEIGHT_TOLERANCE:
        raise ValueError(f"target weights sum to {total:.6f}, above 1.0")
    if min_trade_value < 0:
        raise ValueError(f"min_trade_value must be >= 0, got {min_trade_value}")

    equity = ctx.equity()
    if equity <= 0:
        return RebalancePlan(sells=[], buys=[])

    positions = ctx.positions()
    sells: list[OrderIntent] = []
    buy_candidates: list[tuple[Pair, float, float]] = []  # (pair, quote value, price)
    for pair in sorted(set(positions) | set(targets), key=str):
        price = _last_price(ctx, pair)
        if price is None or price <= 0:
            continue  # no data for this pair yet — leave it alone
        position = positions.get(pair, Position(pair=pair))
        current_value = position.size * price
        target_value = targets.get(pair, 0.0) * equity
        delta = target_value - current_value
        if target_value == 0.0 and position.size > 0:
            sells.append(OrderIntent(pair=pair, side=Side.SELL, size=position.size, reason="rebalance exit"))
        elif delta < 0 and -delta >= min_trade_value:
            size = min(position.size, -delta / price)
            sells.append(OrderIntent(pair=pair, side=Side.SELL, size=size, reason="rebalance reduce"))
        elif delta >= min_trade_value:
            buy_candidates.append((pair, delta, price))

    cash_left = ctx.cash()
    buys: list[OrderIntent] = []
    for pair, value, price in buy_candidates:
        value = min(value, cash_left)
        if value < min_trade_value:
            continue
        buys.append(OrderIntent(pair=pair, side=Side.BUY, size=value / price, reason="rebalance entry"))
        cash_left -= value
    return RebalancePlan(sells=sells, buys=buys)
