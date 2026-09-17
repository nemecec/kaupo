"""Prospective shadow evaluation. A pass requests review, never live trading.

The evaluation window and policy are fixed at registration. Backtests and
pre-registration fills cannot count. Each later run must resume the prior
ledger and retain the frozen trading configuration. Missing evidence fails
closed. Costs are observed EUR research expenses, not the spending ceiling.
"""

import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.core.engine import STOPPED_EXTERNALLY
from kaupo.core.recorder import SUPERSEDED_HALT_REASON, WATCHDOG_HALT_REASON
from kaupo.db.models import EquitySnapshotRow, EventRow, FillRow, ForwardTrialRow, ResearchLedgerRow, RunRow
from kaupo.domain import Timeframe


class ForwardPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    evaluation_days: int = 90
    min_completed_positions: int = 20
    min_position_notional_eur: float = 500.0
    min_sharpe: float = 1.0
    max_drawdown_pct: float = 15.0
    capital_eur: float = 10_000.0
    outer_loss_eur: float = 5_000.0
    monthly_research_budget_eur: float = 100.0
    min_coverage: float = 0.95


def aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def frozen_config(run: RunRow) -> dict[str, Any]:
    transient = {"resumed_from", "chain_started_at", "warmup", "assignment_id"}
    config = {k: v for k, v in run.config.items() if k not in transient}
    return {
        "strategy_id": run.strategy_id,
        "identity": run.config.get("behaviour_hash") or run.strategy_version,
        "config": config,
    }


def signature(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def completed_positions(fills: list[FillRow], min_notional: float = 0) -> tuple[int, bool]:
    """Count flat-to-flat positions, not partial sells. Return ledger validity."""
    sizes: dict[str, Decimal] = defaultdict(Decimal)
    peaks: dict[str, float] = defaultdict(float)
    count = 0
    for fill in fills:
        if not all(math.isfinite(x) and x >= 0 for x in (fill.size, fill.price, fill.fee)):
            return count, False
        size = Decimal(str(fill.size))
        if fill.side == "buy":
            sizes[fill.pair] += size
            peaks[fill.pair] = max(peaks[fill.pair], float(sizes[fill.pair]) * fill.price)
        elif fill.side == "sell":
            previous = sizes[fill.pair]
            sizes[fill.pair] -= size
            if sizes[fill.pair] < Decimal("-0.0000000001"):
                return count, False
            if previous > 0 and abs(sizes[fill.pair]) <= Decimal("0.0000000001"):
                sizes[fill.pair] = Decimal(0)
                count += int(peaks[fill.pair] >= min_notional)
                peaks[fill.pair] = 0
        else:
            return count, False
    return count, True


def costs_covered(rows: list[ResearchLedgerRow], start: datetime, end: datetime) -> bool:
    cursor = start
    for row in sorted((r for r in rows if r.kind == "coverage"), key=lambda r: aware(r.period_start)):
        if aware(row.period_start) > cursor:
            break
        cursor = max(cursor, aware(row.period_end))
        if cursor >= end:
            return True
    return False


def evaluate(
    trial: ForwardTrialRow,
    runs: list[RunRow],
    snapshots: list[EquitySnapshotRow],
    fills: list[FillRow],
    costs: list[ResearchLedgerRow],
    now: datetime,
    events: list[EventRow] | None = None,
) -> dict[str, Any]:
    policy = ForwardPolicy.model_validate(trial.policy)
    start, end = aware(trial.registered_at), aware(trial.ends_at)
    cutoff = min(aware(now), end)
    reasons: list[str] = []
    relevant = sorted(
        (r for r in runs if r.id == trial.root_run_id or start <= aware(r.started_at) < cutoff),
        key=lambda r: (aware(r.started_at), r.id),
    )
    if not relevant or relevant[0].id != trial.root_run_id:
        reasons.append("missing registered run")
    for i, run in enumerate(relevant):
        if run.mode != "shadow" or signature(frozen_config(run)) != trial.signature:
            reasons.append("frozen configuration changed")
        if i and run.config.get("resumed_from") != relevant[i - 1].id:
            reasons.append("ledger continuity broken")
        ended_in_window = run.ended_at is None or aware(run.ended_at) <= cutoff
        if run.status == "failed" and ended_in_window:
            reasons.append("run failed during evaluation")
        allowed_reasons: tuple[str | None, ...] = (None, SUPERSEDED_HALT_REASON, STOPPED_EXTERNALLY)
        if i + 1 < len(relevant) and relevant[i + 1].config.get("resumed_from") == run.id:
            allowed_reasons += (WATCHDOG_HALT_REASON,)
        if ended_in_window and (run.metrics or {}).get("halt_reason") not in allowed_reasons:
            reasons.append("run halted during evaluation")
    ids = {r.id for r in relevant}
    for event in events or []:
        data = event.data or {}
        if (
            data.get("run_id") in ids
            and start <= aware(event.ts) < cutoff
            and data.get("halt_reason") not in (None, SUPERSEDED_HALT_REASON, STOPPED_EXTERNALLY)
        ):
            reasons.append("run halted during evaluation")
    if any(f.run_id in ids and aware(f.ts) < start for f in fills):
        reasons.append("pre-registration activity in forward ledger")
    points = sorted(
        (p for p in snapshots if p.run_id in ids and start <= aware(p.ts) < cutoff),
        key=lambda p: aware(p.ts),
    )
    # Candle snapshots are stamped at candle OPEN. They become evidence only
    # at close; this prevents including an unfinished final candle.
    timeframe = Timeframe.parse(trial.frozen_config["config"]["timeframe"])
    step = timedelta(seconds=timeframe.seconds)
    points = [p for p in points if aware(p.ts) + step <= cutoff]
    selected_fills = sorted(
        (f for f in fills if f.run_id in ids and start <= aware(f.ts) and aware(f.ts) + step <= cutoff),
        key=lambda f: (aware(f.ts), f.side, f.id),
    )
    completed, valid = completed_positions(selected_fills, policy.min_position_notional_eur)
    if not valid:
        reasons.append("invalid forward fill ledger")
    times = [aware(p.ts) + step for p in points]
    if len(set(times)) != len(times):
        reasons.append("overlapping equity snapshots")
    coverage = len(set(times)) * timeframe.seconds / max((cutoff - start).total_seconds(), 1)
    edges = [start, *times, cutoff]
    if coverage < policy.min_coverage or any(b - a > step * 2 for a, b in pairwise(edges)):
        reasons.append("incomplete equity coverage")
    values = [trial.baseline_equity, *(p.equity for p in points)]
    if not all(math.isfinite(v) and v > 0 for v in values):
        reasons.append("invalid equity values")
        values = [trial.baseline_equity]
    # Aggregate to daily marks, avoiding misleading intraday Sharpe inflation.
    daily: dict[str, float] = {}
    for ts, point in zip(times, points, strict=True):
        daily[ts.date().isoformat()] = point.equity
    daily_values = np.array([trial.baseline_equity, *daily.values()], dtype=float)
    sharpe: float | None = None
    if len(daily_values) >= 3 and np.all(np.isfinite(daily_values)) and np.all(daily_values > 0):
        returns = np.diff(daily_values) / daily_values[:-1]
        std = float(returns.std(ddof=1))
        if std > 0:
            sharpe = float(returns.mean() / std * math.sqrt(365.25))
    array = np.array(values)
    max_dd = float(np.max(1 - array / np.maximum.accumulate(array))) * 100
    trading_profit = values[-1] - trial.baseline_equity
    expense = sum(
        float(r.amount_eur) for r in costs if r.kind == "cost" and start <= aware(r.period_start) < cutoff
    )
    coverage_complete = costs_covered(costs, start, cutoff)
    if not coverage_complete:
        reasons.append("research cost coverage not attested")
    if completed < policy.min_completed_positions:
        reasons.append("too few completed forward positions")
    if sharpe is None or sharpe < policy.min_sharpe:
        reasons.append("forward Sharpe below threshold")
    if max_dd > policy.max_drawdown_pct:
        reasons.append("forward drawdown exceeded")
    if min(values) < trial.baseline_equity - policy.outer_loss_eur:
        reasons.append("outer capital loss exceeded")
    if trading_profit - expense <= 0:
        reasons.append("no standalone profit after research costs")
    finished = aware(now) >= end
    invalid = any(
        r in reasons
        for r in (
            "missing registered run",
            "frozen configuration changed",
            "ledger continuity broken",
            "run failed during evaluation",
            "run halted during evaluation",
            "invalid forward fill ledger",
            "pre-registration activity in forward ledger",
            "overlapping equity snapshots",
            "invalid equity values",
        )
    )
    status = "invalidated" if invalid else "collecting"
    if finished and not invalid:
        status = "review_required" if not reasons else "insufficient_evidence"
    return {
        "id": trial.id,
        "assignment_id": trial.assignment_id,
        "status": status,
        "registered_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "as_of": cutoff.isoformat(),
        "hypothesis": trial.hypothesis,
        "policy": trial.policy,
        "frozen_config": trial.frozen_config,
        "reasons": sorted(set(reasons)),
        "completed_positions": completed,
        "equity_points": len(points),
        "coverage_fraction": min(coverage, 1.0),
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "trading_profit_eur": trading_profit,
        "research_cost_eur": expense,
        "cost_coverage_complete": coverage_complete,
        "standalone_net_profit_eur": trading_profit - expense,
        "automatic_live_promotion": False,
        "limitations": [
            "Shadow fills are simulated; live execution still requires a small capital trial.",
            "Sharpe is descriptive, not a multiple-testing-adjusted confidence probability.",
            "All recorded research spend in this window is charged to this standalone candidate.",
            "Pre-registration discovery costs, hosting, and other expenses are excluded.",
            "Reports are recomputed; later accounting corrections can change the verdict.",
        ],
    }


async def trial_report(session: AsyncSession, trial: ForwardTrialRow, now: datetime) -> dict[str, Any]:
    runs = list(
        (
            await session.scalars(
                select(RunRow).where(
                    RunRow.config["assignment_id"].as_string() == trial.assignment_id,
                    RunRow.mode == "shadow",
                )
            )
        ).all()
    )
    ids = [r.id for r in runs]
    points = list(
        (
            await session.scalars(
                select(EquitySnapshotRow).where(
                    EquitySnapshotRow.run_id.in_(ids),
                    EquitySnapshotRow.ts >= trial.registered_at,
                    EquitySnapshotRow.ts < trial.ends_at,
                )
            )
        ).all()
    )
    fills = list(
        (
            await session.scalars(
                select(FillRow).where(
                    FillRow.run_id.in_(ids),
                    FillRow.ts < trial.ends_at,
                )
            )
        ).all()
    )
    costs = list(
        (
            await session.scalars(
                select(ResearchLedgerRow).where(
                    ResearchLedgerRow.period_end >= trial.registered_at,
                    ResearchLedgerRow.period_start < trial.ends_at,
                )
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(EventRow).where(
                    EventRow.source == "engine",
                    EventRow.data["run_id"].as_string().in_(ids),
                    EventRow.ts >= trial.registered_at,
                    EventRow.ts < trial.ends_at,
                )
            )
        ).all()
    )
    report = evaluate(trial, runs, points, fills, costs, now, events)
    report["registered_trials_total"] = await session.scalar(
        select(func.count()).select_from(ForwardTrialRow)
    )
    return report
