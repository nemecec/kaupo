"""Rolling-origin triage: does shadow reality still track the backtest expectation?

For every enabled shadow assignment, the report re-backtests the exact
assignment config against stored candles, and compares the result with the
shadow chain's actual stitched equity and fills over the same slice. The
7-shadow-day promotion gate only counts days; this report catches slow
decay weeks before the gate alone would. It runs daily (05:13 UTC, see
deploy/rolling-report.sh) and persists one row per ISO week into the
reports table, keyed "rolling-origin-<ISO week>" — reruns in the same week
replace the row.

Comparison window: verdicts cover [max(window_start, chain_started_at), now).
The replay starts at the chain origin, including pre-window decisions and
positions, then its persisted equity and fills are sliced to the comparison
window. It never liquidates positions at the end. A replay cannot restore
historical strategy versions, restart-local state, or old fee models; these
remain limitations of the comparison, not evidence of strategy decay.

Venue model: the re-backtest runs the same marketable-limit mode as the
shadow run it is compared against (LIVE_MIRROR_MARKETABLE_LIMIT, kaupo#36),
and every entry records it as "marketable_limit". Rows written before that
change were produced under the legacy maker model, so the key marks the
boundary in the weekly series instead of leaving it silent.

Fee model: the same reasoning applies to the fee tier, which moved from
26/16 bps to the account's real 80/40 (kaupo#44). A sharpe computed under
one tier does not compare with a sharpe computed under another, so every
entry records the rates both sides ran under as "fees". The report passes
them to the backtest explicitly rather than letting the request inherit a
default, so the recorded numbers are the numbers that ran.

Coverage guard: a verdict is emitted only when the chain's overlap with the
window is at least MIN_OVERLAP_DAYS and the chain's in-window equity spans
at least MIN_EQUITY_SPAN_FRACTION of that overlap (a chain that died inside
the window fails the span half). The decision is made before the backtest:
below the floor — or below one full bar of the timeframe, where no closed
candle can sit in the slice (the bar covering the slice start opened
before it) — the backtest is skipped entirely. The entry then carries
"backtest": null, verdict "unknown", and a note ("chain covers Xd of the
Nd window"); no runs row is created, and a sub-candle slice never dies
with a misleading "no candles" error on a healthy young chain.

Verdict rules (thresholds are the module constants below):
- Compare fill counts with fill counts, including a zero/nonzero mismatch.
  Counts differing by more than COUNT_DIVERGENCE_RATIO mean "diverges".
- Compare Sharpes computed on the same observed timestamps, without filling
  missing observations. A gap below/above SHARPE_TOLERANCE means "lags" or
  "leads"; otherwise "tracks". No-activity curves still undergo this check.
- Insufficient common observations, coverage, or invalid equity produce
  "unknown" with a note. Each report retains the original window metrics and
  a separate "comparison" block with the aligned Sharpes and coverage.

The shadow curve stitches the assignment's resume chain, keeping absolute
equity across resume links. The replay runs from the recorded chain origin;
if its observations do not reach that origin, the verdict is unknown because
pre-window signals may be missing. Errors remain isolated to one assignment.

"""

import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.metrics import compute_metrics
from kaupo.backtest.plan import lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.config import get_settings
from kaupo.core.notify import send_alert
from kaupo.data.assignments import Assignment, effective_fees, list_assignments
from kaupo.db.models import EquitySnapshotRow, FillRow, ReportRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Fill, OrderId, Pair, RunMode, Side, Timeframe, new_id, utc_now
from kaupo.market_calendar import TradingSchedule
from kaupo.sdk.protocol import LoadedStrategy
from kaupo.venues.paper import LIVE_MIRROR_MARKETABLE_LIMIT

log = logging.getLogger(__name__)

REPORT_TYPE = "rolling-origin"
DEFAULT_WINDOW_DAYS = 30
# verdict thresholds
SHARPE_TOLERANCE = 0.3
COUNT_DIVERGENCE_RATIO = 2.0
# coverage floor: a verdict needs this much chain overlap with the window,
# with in-window equity spanning at least this fraction of the overlap
MIN_OVERLAP_DAYS = 7.0
MIN_EQUITY_SPAN_FRACTION = 0.5
# mirrors the supervisor's default for assignments without a starting_cash
DEFAULT_STARTING_CASH = 10_000.0

VERDICT_TRACKS = "tracks"
VERDICT_LAGS = "lags"
VERDICT_LEADS = "leads"
VERDICT_DIVERGES = "diverges"
VERDICT_UNKNOWN = "unknown"
VERDICT_ERROR = "error"


def iso_week(now: datetime) -> str:
    """The report's period: the ISO week of ``now``, e.g. "2026-W35"."""
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def period_key(period: str) -> str:
    """The reports-table key of a period: the type discriminator lives in the key."""
    return f"{REPORT_TYPE}-{period}"


def slice_window(
    points: list[tuple[datetime, float]], start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """The points of the curve inside the half-open window [start, end)."""
    return [(ts, value) for ts, value in points if start <= ts < end]


def compare_window(window_start: datetime, chain_start: datetime | None) -> datetime:
    """The comparison start: the chain's start when the chain is younger than the window.

    Both sides must see the same regime, so a chain younger than the window
    compares over its own lifetime only. Without a chain there is nothing
    to align to: the full window stays (the shadow side is the note path).
    """
    if chain_start is None:
        return window_start
    return max(window_start, chain_start)


def has_min_coverage(*, overlap_days: float, span_days: float) -> bool:
    """True when the chain's coverage of the window supports a real verdict.

    The floor: the overlap of the chain with the window is at least
    MIN_OVERLAP_DAYS, and the in-window equity spans at least
    MIN_EQUITY_SPAN_FRACTION of that overlap. Below the floor the report
    emits "unknown" with a coverage note — a young (or dead) chain must not
    look like it lags a backtest that saw a different regime.
    """
    return overlap_days >= MIN_OVERLAP_DAYS and span_days >= overlap_days * MIN_EQUITY_SPAN_FRACTION


def stitch(
    groups: list[list[tuple[datetime, float]]], resumed: list[bool] | None = None
) -> list[tuple[datetime, float]]:
    """One curve from per-run point groups in chain order (root first).

    The same rebase stitch_equity applies, with the same resume distinction:
    a run that resumed its predecessor carries absolute equity and takes no
    offset (``resumed[i]``), while a fresh ledger is rebased onto the previous
    run's last stitched point. Where runs overlap, the later run wins.
    ``resumed`` defaults to all-fresh, the historical behavior.
    """
    flags = resumed if resumed is not None else [False] * len(groups)
    stitched: list[tuple[datetime, float]] = []
    for rows, is_resumed in zip(groups, flags, strict=True):
        if not rows:
            continue
        first_ts = rows[0][0]
        while stitched and stitched[-1][0] >= first_ts:
            stitched.pop()  # overlap: the later run wins
        offset = stitched[-1][1] - rows[0][1] if stitched and not is_resumed else 0.0
        stitched.extend((ts, value + offset) for ts, value in rows)
    return stitched


def verdict_for(
    *, backtest_sharpe: float, backtest_fills: int, shadow_sharpe: float, shadow_fills: int
) -> str:
    """The verdict of one assignment; the rules are in the module docstring."""
    if (shadow_fills == 0) != (backtest_fills == 0):
        return VERDICT_DIVERGES
    if shadow_fills > 0 and backtest_fills > 0:
        bigger = max(shadow_fills, backtest_fills)
        smaller = min(shadow_fills, backtest_fills)
        if bigger > smaller * COUNT_DIVERGENCE_RATIO:
            return VERDICT_DIVERGES
    # sharpes arrive rounded to 3 decimals; rounding the gap keeps the
    # tolerance edges exact (float noise like 1.3 - 1.0 = 0.300...004)
    gap = round(shadow_sharpe - backtest_sharpe, 6)
    if gap < -SHARPE_TOLERANCE:
        return VERDICT_LAGS
    if gap > SHARPE_TOLERANCE:
        return VERDICT_LEADS
    return VERDICT_TRACKS


def _scheduled_coverage(
    first: datetime,
    last: datetime,
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
    schedule: TradingSchedule,
) -> float:
    expected = schedule.open_elapsed(start, end, candle_seconds=timeframe.seconds)
    if expected <= timedelta(0):
        return 0.0
    covered = schedule.open_elapsed(
        max(start, first),
        min(end, last + timedelta(seconds=timeframe.seconds)),
        candle_seconds=timeframe.seconds,
    )
    return covered / expected


def aligned_comparison(
    shadow: list[tuple[datetime, float]],
    backtest: list[tuple[datetime, float]],
    *,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    trading_schedule: TradingSchedule | None = None,
) -> dict[str, Any]:
    """Compare observed equity on one shared grid; never invent missing bars."""
    shadow_by_ts, backtest_by_ts = dict(shadow), dict(backtest)
    common = sorted(shadow_by_ts.keys() & backtest_by_ts.keys())
    union_count = len(shadow_by_ts.keys() | backtest_by_ts.keys())
    coverage = len(common) / union_count if union_count else 0.0
    result: dict[str, Any] = {
        "method": "common-timestamps",
        "shadow_points": len(shadow_by_ts),
        "backtest_points": len(backtest_by_ts),
        "common_points": len(common),
        "coverage_fraction": coverage,
    }
    if len(common) < 3:
        return {
            **result,
            "reason_code": "insufficient_common_observations",
            "note": "fewer than three common equity observations",
        }
    result.update(start=common[0].isoformat(), end=common[-1].isoformat())
    span = (common[-1] - common[0]).total_seconds()
    span_fraction = (
        _scheduled_coverage(common[0], common[-1], start, end, timeframe, trading_schedule)
        if trading_schedule is not None
        else span / (end - start).total_seconds()
        if end > start
        else 0.0
    )
    if coverage < MIN_EQUITY_SPAN_FRACTION or span_fraction < MIN_EQUITY_SPAN_FRACTION:
        return {
            **result,
            "reason_code": "insufficient_common_coverage",
            "note": "insufficient common equity coverage",
        }
    for label, curve in (("shadow", shadow_by_ts), ("backtest", backtest_by_ts)):
        values = [(ts, curve[ts]) for ts in common]
        if any(not math.isfinite(value) or value <= 0 for _, value in values):
            return {
                **result,
                "reason_code": "invalid_common_equity",
                "note": "non-positive or non-finite equity on the common grid",
            }
        metrics = compute_metrics(
            values,
            [],
            timeframe,
            starting_cash=values[0][1],
            trading_schedule=trading_schedule,
            valuation_end=end,
        )
        if "return_sampling" in metrics:
            result["return_sampling"] = metrics["return_sampling"]
        if metrics["sharpe"] is None:
            return {
                **result,
                "reason_code": metrics["return_sampling"]["reason_code"],
                "note": "scheduled daily returns are unavailable on the common grid",
            }
        if not math.isfinite(metrics["sharpe"]):
            return {
                **result,
                "reason_code": "invalid_common_sharpe",
                "note": "non-finite Sharpe on the common grid",
            }
        result[f"{label}_sharpe"] = metrics["sharpe"]
    return result


async def _compare_replay(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    chain: list[RunRow],
    run_id: str,
    replay_start: datetime,
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
    metrics: dict[str, Any],
    trading_schedule: TradingSchedule | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Window the replay and align its curve with the shadow's observed curve."""
    async with sm_scope(sessionmaker) as session:
        rows = (
            await session.scalars(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == run_id)
                .order_by(EquitySnapshotRow.ts)
            )
        ).all()
        replay = [(_aware(row.ts), row.equity) for row in rows]
        shadow = slice_window(await _chain_equity(session, chain), start, end)
        fills = await _chain_window_fills(session, [run_id], start, end)
    windowed = slice_window(replay, start, end)
    comparison = aligned_comparison(
        shadow, windowed, timeframe=timeframe, start=start, end=end, trading_schedule=trading_schedule
    )
    # Crypto bars are continuous. Starting later means the replay could have
    # missed the pre-window signal that created a carried position (#54).
    if not replay or replay[0][0] != replay_start:
        comparison["note"] = "replay history does not reach the chain origin"
        comparison["reason_code"] = "incomplete_replay_history"
    window_metrics = compute_metrics(
        windowed,
        fills,
        timeframe,
        starting_cash=windowed[0][1] if windowed else 1.0,
        universe=metrics.get("universe"),
        trading_schedule=trading_schedule,
        valuation_end=end,
    )
    # Keep run-level status, but never present lifetime risk rejections as a
    # window count. They are available on the referenced replay run.
    window_metrics.pop("risk_rejections", None)
    window_metrics["status"] = metrics.get("status", "completed")
    return window_metrics, comparison


def envelope(
    period: str, days: int, generated_at: datetime, assignments: list[dict[str, Any]]
) -> dict[str, Any]:
    """The report body persisted into reports.body and served to the digest."""
    return {
        "type": REPORT_TYPE,
        "period": period,
        "window_days": days,
        "generated_at": generated_at.isoformat(),
        "assignments": assignments,
    }


def digest_lines(body: dict[str, Any]) -> list[str]:
    """One line per assignment: "<id> <strategy> <pair(s)> <timeframe>: ... — verdict"."""
    lines = []
    for entry in body["assignments"]:
        what = f"{entry['id']} {entry['strategy_id']} {entry['pair']} {entry['timeframe']}"
        if "error" in entry:
            lines.append(f"{what}: error: {entry['error']}")
            continue
        shadow = entry["shadow"]
        if "note" in shadow:
            reason = entry.get("verdict_reason", "unspecified")
            lines.append(f"{what}: unknown [{reason}]: {shadow['note']}")
            continue
        backtest = entry["backtest"]
        comparison = entry.get("comparison", {})
        backtest_sharpe = comparison.get("backtest_sharpe", backtest["sharpe"])
        shadow_sharpe = comparison.get("shadow_sharpe", shadow["sharpe"])
        lines.append(
            f"{what}: backtest sharpe {backtest_sharpe} / shadow {shadow_sharpe}"
            f" ({shadow['num_fills']} fills) — {entry['verdict']}"
        )
    return lines


async def send_digest(body: dict[str, Any]) -> None:
    """Push the digest to the ntfy topic; an ntfy failure never fails the report."""
    header = f"Kaupo rolling-origin {body['period']} ({body['window_days']}d window):"
    try:
        await send_alert("\n".join([header, *digest_lines(body)]))
    except Exception:
        log.warning("rolling-origin ntfy digest failed", exc_info=True)


async def _chain_rows(session: AsyncSession, assignment_id: str) -> list[RunRow]:
    """The resume chain of the assignment's latest shadow run, root first.

    Mirrors resume._chain_rows; a broken link (cycle, missing parent) ends
    the walk with what is there — the report works with partial history.
    """
    stmt = (
        select(RunRow)
        .where(
            RunRow.mode == RunMode.SHADOW.value, RunRow.config["assignment_id"].as_string() == assignment_id
        )
        .order_by(RunRow.started_at.desc(), RunRow.id.desc())
        .limit(1)
    )
    tip = (await session.execute(stmt)).scalars().first()
    if tip is None:
        return []
    chain = [tip]
    seen = {tip.id}
    row = tip
    while (parent_id := (row.config or {}).get("resumed_from")) is not None:
        if parent_id in seen:
            log.warning("Run chain at %s cycles; using the links walked so far", tip.id)
            break
        parent = await session.get(RunRow, parent_id)
        if parent is None:
            log.warning(
                "Run %s resumes from %s, which is gone; using the links walked so far", row.id, parent_id
            )
            break
        chain.append(parent)
        seen.add(parent_id)
        row = parent
    chain.reverse()
    return chain


def _lineage(chain: list[RunRow], first_decision: datetime | None) -> dict[str, Any]:
    """Describe retained ancestry without claiming why any row is absent."""
    if not chain:
        return {"status": "no_shadow_runs"}
    parent_id = (chain[0].config or {}).get("resumed_from")
    status = "complete"
    if parent_id is not None:
        status = "chain_cycle" if parent_id in {run.id for run in chain} else "missing_chain_ancestor"
    elif first_decision is None:
        status = "missing_origin_equity"
    return {
        "status": status,
        "tip_run_id": chain[-1].id,
        "oldest_retained_run_id": chain[0].id,
        "unresolved_parent_run_id": parent_id,
        "retained_run_count": len(chain),
        "first_observed_decision": _aware(first_decision).isoformat() if first_decision is not None else None,
    }


def _chain_start(chain: list[RunRow]) -> datetime | None:
    """The chain's start: the tip's recorded chain_started_at, else the root's start."""
    if not chain:
        return None
    raw = (chain[-1].config or {}).get("chain_started_at") or chain[0].started_at.isoformat()
    ts = datetime.fromisoformat(str(raw))
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _aware(ts: datetime) -> datetime:
    """Timestamps are UTC by domain rule; naive ones appear only from SQLite in tests."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def _chain_equity(session: AsyncSession, chain: list[RunRow]) -> list[tuple[datetime, float]]:
    """The chain's stitched equity curve, ascending by ts."""
    rows = list(
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id.in_([r.id for r in chain]))
                .order_by(EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )
    by_run: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append((_aware(row.ts), row.equity))
    resumed = [(run.config or {}).get("resumed_from") is not None for run in chain]
    return stitch([by_run.get(run.id, []) for run in chain], resumed)


async def _chain_window_fills(
    session: AsyncSession, chain_ids: list[str], start: datetime, end: datetime
) -> list[Fill]:
    """The chain's fills inside [start, end), oldest first (same tie-break as resume)."""
    rows = (
        (
            await session.execute(
                select(FillRow)
                .where(FillRow.run_id.in_(chain_ids), FillRow.ts >= start, FillRow.ts < end)
                .order_by(FillRow.ts, FillRow.side, FillRow.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        Fill(
            order_id=OrderId(row.order_id),
            pair=Pair.parse(row.pair),
            side=Side(row.side),
            ts=row.ts,
            price=row.price,
            size=row.size,
            fee=row.fee,
        )
        for row in rows
    ]


async def _shadow_metrics(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    chain: list[RunRow],
    chain_start: datetime | None,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    now: datetime,
    days: int,
    trading_schedule: TradingSchedule | None = None,
) -> dict[str, Any]:
    """The shadow reality of one assignment over the comparison window, or a note."""
    if not chain:
        return {"reason_code": "no_shadow_runs", "note": "no shadow runs yet"}
    async with sm_scope(sessionmaker) as session:
        equity = await _chain_equity(session, chain)
        fills = await _chain_window_fills(session, [run.id for run in chain], start, end)
    windowed = slice_window(equity, start, end)
    if len(windowed) < 2:
        assert chain_start is not None  # a non-empty chain always has a start
        age_days = max((now - chain_start).total_seconds() / 86400, 0.0)
        return {
            "reason_code": "insufficient_shadow_observations",
            "note": f"chain has {age_days:.1f} days so far",
        }
    metrics = compute_metrics(
        equity=windowed,
        fills=fills,
        timeframe=timeframe,
        starting_cash=windowed[0][1],
        trading_schedule=trading_schedule,
        valuation_end=end,
    )
    sampling = metrics.get("return_sampling", {})
    if "reason_code" in sampling:
        metrics["reason_code"] = sampling["reason_code"]
        metrics["note"] = "scheduled daily returns are unavailable"
    overlap_seconds = (now - start).total_seconds()
    overlap_days = overlap_seconds / 86400
    span_days = (windowed[-1][0] - windowed[0][0]).total_seconds() / 86400
    # belt and braces: below one full bar no closed candle can sit in the
    # slice (the bar covering the slice start opened before it), so the
    # backtest would find zero candles and die with a misleading error
    enough_coverage = has_min_coverage(overlap_days=overlap_days, span_days=span_days)
    if trading_schedule is not None:
        fraction = _scheduled_coverage(
            windowed[0][0], windowed[-1][0], start, end, timeframe, trading_schedule
        )
        enough_coverage = overlap_days >= MIN_OVERLAP_DAYS and fraction >= MIN_EQUITY_SPAN_FRACTION
    if overlap_seconds < timeframe.seconds or not enough_coverage:
        # the metrics stay in the entry; the note suppresses the verdict
        metrics["note"] = f"chain covers {span_days:.1f}d of the {days}d window"
        metrics["reason_code"] = "insufficient_shadow_coverage"
    return metrics


async def _assignment_entry(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    strategies: dict[str, LoadedStrategy] | None,
    load_error: str | None,
    assignment: Assignment,
    period: str,
    start: datetime,
    end: datetime,
    now: datetime,
    days: int,
) -> dict[str, Any]:
    """One report entry: shadow reality vs backtest expectation, plus the verdict."""
    cash = assignment.starting_cash or DEFAULT_STARTING_CASH
    async with sm_scope(sessionmaker) as session:
        chain = await _chain_rows(session, assignment.id)
        first_decision = (
            await session.scalar(
                select(func.min(EquitySnapshotRow.ts)).where(EquitySnapshotRow.run_id == chain[0].id)
            )
            if chain
            else None
        )
    chain_start = _chain_start(chain)
    compare_start = compare_window(start, chain_start)
    # Run creation is wall-clock time; decisions are stamped with candle
    # OPEN times. A 10:15 startup can first trade the 10:00 candle (#54).
    replay_start = _aware(first_decision) if first_decision is not None else chain_start or compare_start
    taker_bps, maker_bps = effective_fees(assignment)
    entry: dict[str, Any] = {
        "id": assignment.id,
        "asset_class": assignment.asset_class,
        "strategy_id": assignment.strategy_id,
        "pair": assignment.pair,
        "pairs": assignment.pairs,
        "timeframe": assignment.timeframe,
        "start": compare_start.isoformat(),
        "replay_start": replay_start.isoformat(),
        "lineage": _lineage(chain, first_decision),
        "liquidate_end": False,
        # the venue model both sides run under. Every entry before the kaupo#36
        # deploy is maker-mode and every entry after is skip-mode; marking it
        # here keeps that boundary visible in the series the kill decisions read
        "marketable_limit": LIVE_MIRROR_MARKETABLE_LIMIT,
        # the fee tier both sides run under, for the same reason: the 26/16
        # to 80/40 move (kaupo#44) shifts every sharpe in the series, and a
        # silent shift reads as strategy decay to whoever kills the strategy
        "fees": {"taker_bps": taker_bps, "maker_bps": maker_bps},
        **(
            {"metric_schedule": assignment.trading_schedule.model_dump(mode="json")}
            if assignment.trading_schedule is not None
            else {}
        ),
    }
    try:
        timeframe = Timeframe.parse(assignment.timeframe)
    except ValueError as exc:
        return {**entry, "error": str(exc), "verdict": VERDICT_ERROR}

    # the shadow side first: its coverage decides whether a backtest is
    # meaningful at all, before any runs row is created
    shadow = await _shadow_metrics(
        sessionmaker,
        chain=chain,
        chain_start=chain_start,
        timeframe=timeframe,
        start=compare_start,
        end=end,
        now=now,
        days=days,
        trading_schedule=assignment.trading_schedule,
    )
    entry["shadow"] = shadow
    if any((row.config or {}).get("asset_class", "currency") != assignment.asset_class for row in chain):
        shadow["reason_code"] = "asset_class_mismatch"
        shadow["note"] = "recorded chain asset class differs from the assignment"
        entry["backtest"] = None
        entry["verdict"] = VERDICT_UNKNOWN
        entry["verdict_reason"] = "asset_class_mismatch"
        return entry
    lineage = entry["lineage"]
    if lineage["status"] in ("missing_chain_ancestor", "chain_cycle", "missing_origin_equity"):
        # Structural gaps take precedence over a short window: waiting for
        # more recent observations cannot repair a missing origin.
        shadow["reason_code"] = lineage["status"]
        if lineage["status"] == "missing_chain_ancestor":
            shadow["note"] = (
                f"chain origin unavailable: missing ancestor run {lineage['unresolved_parent_run_id']}"
            )
        elif lineage["status"] == "chain_cycle":
            shadow["note"] = (
                f"chain origin unavailable: resume cycle at run {lineage['unresolved_parent_run_id']}"
            )
        else:
            shadow["note"] = (
                "chain origin unavailable: no equity observations for run "
                f"{lineage['oldest_retained_run_id']}"
            )
    if chain and "note" in shadow:
        # coverage below the verdict floor (or under one full bar): the
        # backtest would compare a different regime or find no closed
        # candle — skip it (no runs row, no misleading error)
        entry["backtest"] = None
        entry["verdict"] = VERDICT_UNKNOWN
        entry["verdict_reason"] = shadow["reason_code"]
        return entry

    # A constant-fee replay cannot reproduce a chain that changed schedules.
    # Check the whole replay prefix, not only the displayed comparison window.
    expected_fees = {"taker_bps": taker_bps, "maker_bps": maker_bps}
    fee_mismatches = []
    for row in chain:
        recorded = (row.config or {}).get("fees")
        if not isinstance(recorded, dict) or any(
            isinstance(recorded.get(key), bool) or recorded.get(key) != value
            for key, value in expected_fees.items()
        ):
            fee_mismatches.append(row.id)
    if fee_mismatches:
        shadow["reason_code"] = "fee_schedule_mismatch"
        shadow["note"] = "recorded chain fees differ from the replay schedule or are unavailable"
        entry["fee_mismatch_run_ids"] = fee_mismatches
        entry["backtest"] = None
        entry["verdict"] = VERDICT_UNKNOWN
        entry["verdict_reason"] = "fee_schedule_mismatch"
        return entry

    try:
        if load_error is not None:
            raise ValueError(f"strategies did not load: {load_error}")
        assert strategies is not None
        loaded = strategies.get(assignment.strategy_id)
        if loaded is None:
            raise ValueError(f"unknown strategy {assignment.strategy_id!r}")
        marker = {"period": period, "assignment": assignment.id, "start": compare_start.isoformat()}
        if assignment.pairs is not None:
            portfolio_request = PortfolioBacktestRequest(
                strategy=loaded,
                params=assignment.params,
                pairs=[Pair.parse(p) for p in assignment.pairs],
                timeframe=timeframe,
                start=replay_start,
                end=end,
                starting_cash=cash,
                rolling_origin=marker,
                liquidate_end=False,
                # the same venue model the shadow run uses, or the triage
                # compares two different venues and its verdicts are noise
                marketable_limit=LIVE_MIRROR_MARKETABLE_LIMIT,
                # named, not inherited, so the entry's "fees" stamp is the
                # tier that actually ran
                trading_schedule=assignment.trading_schedule,
                taker_fee_bps=taker_bps,
                maker_fee_bps=maker_bps,
            )
            run_id, _, metrics = await run_portfolio_backtest(portfolio_request, sessionmaker)
        else:
            request = BacktestRequest(
                strategy=loaded,
                params=assignment.params,
                pair=Pair.parse(assignment.pair),
                timeframe=timeframe,
                start=replay_start,
                end=end,
                starting_cash=cash,
                rolling_origin=marker,
                liquidate_end=False,
                marketable_limit=LIVE_MIRROR_MARKETABLE_LIMIT,  # as in run_shadow
                trading_schedule=assignment.trading_schedule,
                taker_fee_bps=taker_bps,  # as in the entry's "fees" stamp
                maker_fee_bps=maker_bps,
            )
            run_id, _, metrics = await run_backtest(request, sessionmaker)
    except Exception as exc:
        log.warning("rolling-origin backtest failed for assignment %s", assignment.id, exc_info=True)
        return {**entry, "error": str(exc), "verdict": VERDICT_ERROR}
    entry["backtest"] = {"run_id": str(run_id), **metrics}
    if "error" in metrics:
        return {**entry, "error": str(metrics["error"]), "verdict": VERDICT_ERROR}
    if "note" in shadow:
        entry["verdict"] = VERDICT_UNKNOWN
        entry["verdict_reason"] = shadow["reason_code"]
        return entry
    try:
        window_metrics, comparison = await _compare_replay(
            sessionmaker,
            chain=chain,
            run_id=str(run_id),
            replay_start=replay_start,
            start=compare_start,
            end=end,
            timeframe=timeframe,
            metrics=metrics,
            trading_schedule=assignment.trading_schedule,
        )
    except Exception as exc:
        log.warning("rolling-origin comparison failed for %s", assignment.id, exc_info=True)
        return {**entry, "error": str(exc), "verdict": VERDICT_ERROR}
    entry["backtest"] = {"run_id": str(run_id), **window_metrics}
    entry["comparison"] = comparison
    if "note" in comparison:
        shadow["note"] = comparison["note"]
        entry["verdict"] = VERDICT_UNKNOWN
        entry["verdict_reason"] = comparison["reason_code"]
    else:
        entry["verdict"] = verdict_for(
            backtest_sharpe=float(comparison["backtest_sharpe"]),
            backtest_fills=int(window_metrics["num_fills"]),
            shadow_sharpe=float(comparison["shadow_sharpe"]),
            shadow_fills=int(shadow["num_fills"]),
        )
    return entry


async def build_rolling_origin_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    persist: bool = True,
    strategies_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the rolling-origin report over [now - days, now] and persist it.

    One upsert per ISO week (the period key carries the report type), so a
    rerun in the same week replaces the row. ``now`` is injectable for
    tests; the wall clock is the default.
    """
    now = now or utc_now()
    start = now - timedelta(days=days)
    period = iso_week(now)

    strategies: dict[str, LoadedStrategy] | None = None
    load_error: str | None = None
    try:
        strategies = lint_and_load_strategies(strategies_dir or get_settings().strategies_dir)
    except Exception as exc:  # lint failure, missing directory: every entry degrades, the report completes
        log.warning("rolling-origin strategy load failed", exc_info=True)
        load_error = str(exc) or type(exc).__name__

    async with sm_scope(sessionmaker) as session:
        assignments = await list_assignments(session, enabled_only=True)
    shadow_assignments = [a for a in assignments if a.mode == RunMode.SHADOW.value]
    entries = [
        await _assignment_entry(
            sessionmaker,
            strategies=strategies,
            load_error=load_error,
            assignment=assignment,
            period=period,
            start=start,
            end=now,
            now=now,
            days=days,
        )
        for assignment in shadow_assignments
    ]
    body = envelope(period, days, utc_now(), entries)

    if persist:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with sm_scope(sessionmaker) as session:
            stmt = pg_insert(ReportRow).values(
                id=new_id(), ts=utc_now(), period=period_key(period), run_id=None, body=body
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["period"],
                set_={"ts": stmt.excluded.ts, "body": stmt.excluded.body},
            )
            await session.execute(stmt)
    return body
