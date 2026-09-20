"""Prospective shadow evaluation. A pass requests review, never live trading.

The evaluation window and policy are fixed at registration. Backtests and
pre-registration fills cannot count. Each later run must resume the prior
ledger and retain the frozen trading configuration. Missing evidence fails
closed. Costs are observed EUR research expenses, not the spending ceiling.

A reference backtest can only size the window ahead of time: it says how
often this configuration completes a substantial position, so a trial is
long enough for the forward gate to be reachable. That is planning input,
never evidence — no historical trade counts towards any gate.

The rate transfers only if the reference measured what the shadow run
executes: the same strategy behaviour, the same market, the same engine
build, and costs and risk limits the shadow cannot beat. plan_match_defects
holds that line. Even then the horizon is an estimate from one window, and
a reference on another venue's history stays an estimate the operator reads
rather than a guarantee the planner enforces.
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

from kaupo.config import default_maker_bps, default_taker_bps
from kaupo.core.engine import STOPPED_EXTERNALLY
from kaupo.core.provenance import engine_version
from kaupo.core.recorder import SUPERSEDED_HALT_REASON, WATCHDOG_HALT_REASON
from kaupo.db.models import EquitySnapshotRow, EventRow, FillRow, ForwardTrialRow, ResearchLedgerRow, RunRow
from kaupo.domain import RunMode, RunStatus, Timeframe

# Version 1 trials were registered with a fixed 90-day window, which is too
# short for a slow strategy to complete 20 positions. Version 2 sets the
# window from a reference backtest at registration and never shortens it.
POLICY_VERSION = 2
# The planner's estimate carries sampling error of roughly 1/sqrt(positions).
# This margin covers about one standard error at the minimum sample below.
PLAN_MARGIN = 1.5
PLAN_MIN_HORIZON_DAYS = 365
# Beyond this the candidate is not testable in a useful time. Say so instead
# of shortening the window, which would only weaken the evidence.
PLAN_MAX_HORIZON_DAYS = 1825
PLAN_MIN_REFERENCE_WINDOW_DAYS = 180.0
PLAN_MIN_REFERENCE_POSITIONS = 5
PLAN_MIN_WINDOW_COVERAGE = 0.9
SECONDS_PER_DAY = 86_400.0
DAYS_PER_YEAR = 365.25
# Shadow and live runs both execute on Kraken candles (``run_shadow`` polls a
# ``KrakenClient`` and warms up from the Kraken store). A backtest names the
# venue whose history it replayed in ``config["exchange"]``.
EXECUTION_EXCHANGE = "kraken"
# Cost fields a reference and a run must both record before they compare.
FEE_BPS_KEYS = ("taker_bps", "maker_bps", "slippage_bps")
# Risk fields that only mirror the venue's rates. They are compared as fees,
# under the direction rule, not for equality. See _cost_defects.
RISK_MIRRORED_KEYS = ("taker_fee_bps", "slippage_bps")


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
    # version 2 only: why evaluation_days was chosen. Absent on version 1
    # rows, which keep their stored 90-day window unchanged.
    plan: dict[str, Any] | None = None


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


def _positive_bps(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def _bps(value: Any) -> float | None:
    """A recorded cost rate. ``None`` means the run recorded nothing usable.

    Zero is a real rate here, unlike in ``_positive_bps``: a backtest can
    legitimately charge no slippage, and that still compares.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _configured_days(config: dict[str, Any]) -> float | None:
    """The window the backtest was asked for, read back from its own request."""
    try:
        start = datetime.fromisoformat(str(config["start"]))
        end = datetime.fromisoformat(str(config["end"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (aware(end) - aware(start)).total_seconds() / SECONDS_PER_DAY


def effective_risk(config: dict[str, Any]) -> dict[str, Any] | None:
    """The risk limits a stored run actually enforced, in one shape.

    The two run kinds record risk differently. A backtest stores the
    synchronised configuration, with the venue's fee and slippage already
    folded in (``kaupo/backtest/run.py``). A shadow run stores the
    configuration as requested and folds the same two fields in only when
    it builds its risk manager (``kaupo/core/runner.py``). Reading both
    fields back from ``fees`` puts the two representations side by side.
    ``None`` means the run recorded no risk limits to compare.
    """
    risk = config.get("risk")
    if not isinstance(risk, dict) or not risk:
        return None
    fees = config.get("fees") or {}
    synced = dict(risk)
    for risk_key, fee_key in (("taker_fee_bps", "taker_bps"), ("slippage_bps", "slippage_bps")):
        if risk_key in synced and fee_key in fees:
            synced[risk_key] = fees[fee_key]
    return synced


def reference_defects(run: RunRow, policy: ForwardPolicy) -> list[str]:
    """Why this run cannot size a forward trial. An empty list means it can.

    The reference must be one completed spot backtest over its whole
    requested window, priced at no less than today's fees and filled the
    way a shadow run fills. A sweep or stability slice is excluded: the
    best point of a grid is a selection, so its trade rate is not the rate
    this configuration produces on unseen data.
    """
    config = run.config or {}
    fees = config.get("fees") or {}
    taker, maker = _positive_bps(fees.get("taker_bps")), _positive_bps(fees.get("maker_bps"))
    defects: list[str] = []
    if run.mode != RunMode.BACKTEST.value:
        defects.append("reference must be a backtest run")
    if run.status != RunStatus.COMPLETED.value:
        defects.append("reference backtest did not complete")
    if (run.metrics or {}).get("halt_reason"):
        defects.append("reference backtest halted before the end of its window")
    if config.get("instrument", "spot") != "spot":
        defects.append("reference must be a spot backtest")
    for marker in ("sweep", "stability", "rolling_origin"):
        if config.get(marker):
            defects.append(f"reference must be a standalone backtest, not one {marker} slice")
    if taker is None or maker is None:
        defects.append("reference must charge positive maker and taker fees")
    elif taker < default_taker_bps() or maker < default_maker_bps():
        defects.append(
            f"reference fees are below the current schedule ({default_taker_bps():g} bps taker, "
            f"{default_maker_bps():g} bps maker)"
        )
    if fees.get("marketable_limit") != "skip":
        defects.append('reference must use the live-mirror fill model ("marketable_limit": "skip")')
    if config.get("starting_cash") != policy.capital_eur:
        defects.append(f"reference must start from the {policy.capital_eur:g} EUR baseline")
    pairs = config.get("pairs") or [config.get("pair", "")]
    if any(not str(pair).endswith("/EUR") for pair in pairs):
        defects.append("reference must trade EUR pairs")
    return defects


def trial_plan(
    run: RunRow,
    fills: list[FillRow],
    equity_window: tuple[datetime, datetime, int] | None,
    policy: ForwardPolicy,
) -> dict[str, Any]:
    """How long a forward trial on this configuration has to run.

    The rate comes from the window the reference backtest actually
    recorded, not the window it requested, and the horizon it implies is
    multiplied by a margin. Both choices lengthen the trial rather than
    shorten it. ``blockers`` is empty only when the reference can carry a
    registration; the horizon is never trimmed to make it empty.
    """
    config = run.config or {}
    blockers = reference_defects(run, policy)
    try:
        step = timedelta(seconds=Timeframe.parse(str(config.get("timeframe", ""))).seconds)
    except ValueError:
        step = None
        blockers.append("reference run records no usable timeframe")
    # A backtest without a readable start and end cannot be checked for
    # truncation: a run that stopped a month in would look complete. Say so
    # instead of skipping the check.
    requested = _configured_days(config)
    if requested is None:
        blockers.append("reference backtest does not record the date window it requested")
    elif requested <= 0:
        blockers.append("reference backtest requested an empty date window")
    window_start = window_end = None
    window_days: float | None = None
    if equity_window is None or step is None:
        blockers.append("reference backtest recorded no equity history")
    else:
        first, last, snapshots = equity_window
        window_start, window_end = aware(first), aware(last) + step
        window_days = (window_end - window_start).total_seconds() / SECONDS_PER_DAY
        observed = snapshots * step.total_seconds() / SECONDS_PER_DAY
        if observed < window_days * PLAN_MIN_WINDOW_COVERAGE:
            blockers.append("reference backtest has gaps in its equity record")
        if requested is not None and requested > 0 and window_days < requested * PLAN_MIN_WINDOW_COVERAGE:
            blockers.append("reference backtest covers less of the market than it requested")
        if window_days < PLAN_MIN_REFERENCE_WINDOW_DAYS:
            blockers.append(
                f"reference backtest covers {window_days:.0f} days; "
                f"at least {PLAN_MIN_REFERENCE_WINDOW_DAYS:.0f} are needed to estimate a trade rate"
            )
    ordered = sorted(fills, key=lambda f: (aware(f.ts), f.side, f.id))
    completed, valid = completed_positions(ordered, policy.min_position_notional_eur)
    if not valid:
        blockers.append("reference fill ledger is not a valid flat-to-flat sequence")
        completed = 0
    if completed < PLAN_MIN_REFERENCE_POSITIONS:
        blockers.append(
            f"reference completed {completed} position(s) of at least "
            f"{policy.min_position_notional_eur:g} EUR; at least {PLAN_MIN_REFERENCE_POSITIONS} are needed"
        )
    positions_per_year: float | None = None
    days_per_gate: float | None = None
    required_days: int | None = None
    horizon_days: int | None = None
    if window_days is not None and window_days > 0 and completed > 0:
        positions_per_year = completed * DAYS_PER_YEAR / window_days
        days_per_gate = policy.min_completed_positions * window_days / completed
        required_days = math.ceil(days_per_gate * PLAN_MARGIN)
        horizon_days = max(required_days, PLAN_MIN_HORIZON_DAYS)
        if horizon_days > PLAN_MAX_HORIZON_DAYS:
            # Not an invitation to trade bigger. A larger position clears the
            # notional bar sooner but raises the risk the trial is meant to
            # measure, so the honest answer is that the evidence is out of reach.
            blockers.append(
                f"this configuration needs about {required_days} days to complete "
                f"{policy.min_completed_positions} positions, beyond the {PLAN_MAX_HORIZON_DAYS}-day cap; "
                "it cannot produce enough forward evidence in a testable horizon"
            )
            horizon_days = None
    reference_exchange = str(config.get("exchange") or EXECUTION_EXCHANGE)
    warnings: list[str] = []
    if reference_exchange != EXECUTION_EXCHANGE:
        warnings.append(
            f"the reference replayed {reference_exchange} history and a shadow run trades "
            f"{EXECUTION_EXCHANGE}; the two venues can differ in trade frequency"
        )
    limitations = [
        "The trade rate comes from one historical window. Future frequency can differ.",
        "The horizon is an estimate, not a guarantee that the gate becomes reachable.",
    ]
    if not config.get("engine_version"):
        limitations.append(
            "The reference does not record the engine version it ran under. "
            "Fill-model equivalence rests on its recorded fees, risk limits and fill model."
        )
    return {
        "reference_run_id": run.id,
        "strategy_id": run.strategy_id,
        "pair": config.get("pair"),
        "pairs": config.get("pairs"),
        "timeframe": config.get("timeframe"),
        "params": config.get("params"),
        # What the run has to reproduce before this rate describes it. See
        # plan_match_defects: identity, execution code, costs and limits.
        "reference_strategy_version": run.strategy_version,
        "reference_behaviour_hash": config.get("behaviour_hash"),
        "reference_engine_version": config.get("engine_version"),
        "reference_fees": config.get("fees"),
        "reference_risk": effective_risk(config),
        "reference_starting_cash": config.get("starting_cash"),
        "reference_exchange": reference_exchange,
        "execution_exchange": EXECUTION_EXCHANGE,
        "engine_version": config.get("engine_version") or engine_version(),
        "requested_days": requested,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "window_days": window_days,
        "reference_completed_positions": completed,
        "min_position_notional_eur": policy.min_position_notional_eur,
        "positions_per_year": positions_per_year,
        "days_for_min_positions": days_per_gate,
        "margin": PLAN_MARGIN,
        "required_horizon_days": required_days,
        "min_horizon_days": PLAN_MIN_HORIZON_DAYS,
        "max_horizon_days": PLAN_MAX_HORIZON_DAYS,
        "horizon_days": horizon_days,
        "rate_basis": "estimate",
        "usable": not blockers,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "limitations": limitations,
        "note": (
            "Historical fills size the window only. They are not forward evidence "
            "and no gate is relaxed to fit them."
        ),
    }


def _identity_defects(plan: dict[str, Any], run: RunRow) -> list[str]:
    """Whether the plan measured the same strategy code this run executes.

    The behaviour hash decides when both sides recorded one, because a
    docstring edit leaves it unchanged. A backtest records no behaviour
    hash today, so the source version carries the comparison instead. When
    neither field is on both sides there is nothing to compare, and an
    unverifiable identity is a mismatch.
    """
    config = run.config or {}
    reference, running = plan.get("reference_behaviour_hash"), config.get("behaviour_hash")
    if reference and running:
        return [] if reference == running else ["reference measured different strategy behaviour"]
    reference, running = plan.get("reference_strategy_version"), run.strategy_version
    if not reference or not running:
        return ["reference or run records no strategy identity to compare"]
    return [] if reference == running else ["reference measured a different strategy source version"]


def _engine_defects(plan: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Whether the plan and the run share the execution code that fills orders.

    ``engine_version`` fingerprints the engine, venue, risk and ledger
    modules. The plan retains the reference engine version, so an API-only
    release does not change the identity of the trading runtime.
    """
    planned, running = plan.get("engine_version"), config.get("engine_version")
    if not planned or not running:
        return ["plan or run records no engine version to compare"]
    reference = plan.get("reference_engine_version")
    if reference and reference != running:
        return ["reference ran under a different engine version"]
    if planned != running:
        return ["the plan was computed under a different engine version"]
    return []


def _cost_defects(plan: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Whether the reference priced and constrained trading as this run does.

    Both decide how often a position reaches the notional the gate counts.
    Different pricing can change risk exits and entry frequency in either
    direction, so all execution costs must match. The other risk limits
    must match outright: each of them moves the rate in its own direction,
    so being "close" carries no safe reading.
    """
    defects: list[str] = []
    if plan.get("reference_starting_cash") != config.get("starting_cash"):
        defects.append("reference started from a different cash balance")
    reference_fees, fees = plan.get("reference_fees") or {}, config.get("fees") or {}
    fill_model = fees.get("marketable_limit")
    if not fill_model or reference_fees.get("marketable_limit") != fill_model:
        defects.append("reference used a different fill model")
    for key in FEE_BPS_KEYS:
        reference, running = _bps(reference_fees.get(key)), _bps(fees.get(key))
        if reference is None or running is None:
            defects.append(f"reference or run records no {key} to compare")
        elif reference < running:
            defects.append(f"reference priced {key} below this run")
        elif reference > running:
            defects.append(f"reference priced {key} above this run")
    reference_risk, risk = plan.get("reference_risk"), effective_risk(config)
    if not reference_risk or not risk:
        defects.append("reference or run records no risk limits to compare")
    elif _comparable_limits(reference_risk) != _comparable_limits(risk):
        defects.append("reference ran under different effective risk limits")
    return defects


def _comparable_limits(risk: dict[str, Any]) -> dict[str, Any]:
    """Risk limits without the venue rates already checked as fees."""
    return {k: v for k, v in risk.items() if k not in RISK_MIRRORED_KEYS}


def plan_match_defects(plan: dict[str, Any], run: RunRow) -> list[str]:
    """Why this plan cannot size a trial for this run. Empty means it can.

    A trade rate describes one configuration: one strategy behaviour, one
    market, one execution build, one set of costs and limits. A run that
    differs in any of these completes qualifying positions at its own rate,
    so the plan's horizon would not be the horizon this run needs.
    """
    config = run.config or {}
    defects: list[str] = []
    if plan.get("strategy_id") != run.strategy_id:
        defects.append("reference measured a different strategy id")
    if plan.get("params") != config.get("params"):
        defects.append("reference measured different strategy parameters")
    if plan.get("timeframe") != config.get("timeframe"):
        defects.append("reference measured a different timeframe")
    if plan.get("pair") != config.get("pair") or (plan.get("pairs") or None) != (config.get("pairs") or None):
        defects.append("reference measured a different pair universe")
    defects.extend(_identity_defects(plan, run))
    defects.extend(_engine_defects(plan, config))
    defects.extend(_cost_defects(plan, config))
    return sorted(set(defects))


def plan_matches_run(plan: dict[str, Any], run: RunRow) -> bool:
    """True when this plan can size a forward trial for this run."""
    return not plan_match_defects(plan, run)


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
