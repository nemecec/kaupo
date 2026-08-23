"""Backtest performance metrics from an equity curve and fills."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from kaupo.domain import Fill, Side, Timeframe


@dataclass(frozen=True)
class RoundTrip:
    pnl: float
    fees: float


def round_trips(fills: list[Fill]) -> list[RoundTrip]:
    """FIFO pairing: each sell closes open quantity at average cost.

    PnL per trip = closed_qty * (sell_price - avg_buy_price) - allocated fees.
    """
    trips: list[RoundTrip] = []
    open_qty = 0.0
    open_cost = 0.0  # total quote spent on open_qty (incl. buy fees)
    for fill in fills:
        if fill.side is Side.BUY:
            open_qty += fill.size
            open_cost += fill.quote_amount + fill.fee
        else:
            if open_qty <= 0:
                continue
            closed = min(fill.size, open_qty)
            avg_cost = open_cost / open_qty
            fee_share = fill.fee * (closed / fill.size)
            pnl = closed * (fill.price - avg_cost) - fee_share
            trips.append(RoundTrip(pnl=pnl, fees=fee_share))
            open_qty -= closed
            open_cost -= closed * avg_cost
    return trips


def compute_metrics(
    equity: list[tuple[datetime, float]],
    fills: list[Fill],
    timeframe: Timeframe,
    starting_cash: float,
    risk_rejections: int = 0,
) -> dict[str, Any]:
    if len(equity) < 2:
        return {"error": "not enough data", "num_fills": len(fills)}

    values = np.array([e for _, e in equity], dtype=np.float64)
    final = values[-1]
    total_return = (final - starting_cash) / starting_cash

    days = max((equity[-1][0] - equity[0][0]).total_seconds() / 86400, 1e-9)
    years = days / 365.25
    cagr = (final / starting_cash) ** (1 / years) - 1 if years > 0 and final > 0 else 0.0

    returns = np.diff(values) / values[:-1]
    ppy = timeframe.periods_per_year
    std = returns.std(ddof=1)
    sharpe = float(returns.mean() / std * math.sqrt(ppy)) if std > 0 else 0.0
    downside = returns[returns < 0]
    downside_std = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = float(returns.mean() / downside_std * math.sqrt(ppy)) if downside_std > 0 else 0.0

    peak = np.maximum.accumulate(values)
    drawdowns = (values - peak) / peak
    max_dd = float(drawdowns.min())

    trips = round_trips(fills)
    wins = [t.pnl for t in trips if t.pnl > 0]
    losses = [t.pnl for t in trips if t.pnl <= 0]
    total_fees = sum(f.fee for f in fills)

    return {
        "starting_equity": round(starting_cash, 2),
        "final_equity": round(float(final), 2),
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "num_fills": len(fills),
        "num_round_trips": len(trips),
        "win_rate_pct": round(len(wins) / len(trips) * 100, 1) if trips else None,
        "avg_win": round(float(np.mean(wins)), 2) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "total_fees": round(total_fees, 2),
        "days": round(days, 1),
        "risk_rejections": risk_rejections,
    }
