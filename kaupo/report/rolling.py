"""Rolling-origin triage: does shadow reality still track the backtest expectation?

For every enabled shadow assignment, the report re-backtests the exact
assignment config against stored candles, and compares the result with the
shadow chain's actual stitched equity and fills over the same slice. The
7-shadow-day promotion gate only counts days; this report catches slow
decay weeks before the gate alone would. It runs daily (05:13 UTC, see
deploy/rolling-report.sh) and persists one row per ISO week into the
reports table, keyed "rolling-origin-<ISO week>" — reruns in the same week
replace the row.

Comparison window: both sides see the same regime. The backtest runs over
[max(window_start, chain_started_at), now] — the full ``days`` window only
when the assignment has no chain yet — and the entry's "start" (also the
runs-row marker's "start") records the slice used. Without this, a healthy
young chain looks like it lags: the full-window backtest shows trades that
fired before the chain existed (issue #25).

Coverage guard: a verdict is emitted only when the chain's overlap with the
window is at least MIN_OVERLAP_DAYS and the chain's in-window equity spans
at least MIN_EQUITY_SPAN_FRACTION of that overlap (a chain that died inside
the window fails the span half). Below the floor the entry gets verdict
"unknown" with a note ("chain covers Xd of the Nd window") instead of a
verdict — a Sharpe comparison over a day or two is noise.

Verdict rules (thresholds are the module constants below):
- "tracks" — the shadow chain has no fills and the backtest has no round
  trips, or the shadow sharpe is within SHARPE_TOLERANCE of the backtest
  sharpe;
- "lags" — the shadow sharpe is more than SHARPE_TOLERANCE below the
  backtest sharpe;
- "leads" — the shadow sharpe is more than SHARPE_TOLERANCE above the
  backtest sharpe;
- "diverges" — both sides have activity but the shadow fill count and the
  backtest round-trip count differ by more than COUNT_DIVERGENCE_RATIO
  (checked before the sharpe rules; one round trip is two fills, so an
  exact shadow of the backtest sits at a ratio of 2 and is not flagged).

Two non-comparison outcomes: "unknown" when there is nothing meaningful to
compare (no chain yet, no equity in the window yet, or coverage below the
guard floor — the entry carries a note instead of pretending), and "error"
when the backtest side failed (no candles, a strategy that does not load,
a lint failure). A failed assignment never fails the report: the rest of
the entries still build.

The shadow side feeds the chain's stitched equity (rebased onto the resume
chain, root first — the same rebase stitch_equity applies, but keyed on the
assignment's chain so two assignments sharing a strategy stay separate) and
the chain's in-window fills through the same compute_metrics the backtest
uses, with the assignment's starting cash. Sharpe is rebase-invariant, so
the comparison is meaningful even when the chain predates the window.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.metrics import compute_metrics
from kaupo.backtest.plan import lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.config import get_settings
from kaupo.core.notify import send_alert
from kaupo.data.assignments import Assignment, list_assignments
from kaupo.db.models import EquitySnapshotRow, FillRow, ReportRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Fill, OrderId, Pair, RunMode, Side, Timeframe, new_id, utc_now
from kaupo.sdk.protocol import LoadedStrategy

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


def stitch(groups: list[list[tuple[datetime, float]]]) -> list[tuple[datetime, float]]:
    """One curve from per-run point groups in chain order (root first).

    The same rebase stitch_equity applies: each run's points are offset so
    the run's first point equals the previous run's last stitched point;
    where runs overlap, the later run wins.
    """
    stitched: list[tuple[datetime, float]] = []
    for rows in groups:
        if not rows:
            continue
        first_ts = rows[0][0]
        while stitched and stitched[-1][0] >= first_ts:
            stitched.pop()  # overlap: the later run wins
        offset = stitched[-1][1] - rows[0][1] if stitched else 0.0
        stitched.extend((ts, value + offset) for ts, value in rows)
    return stitched


def verdict_for(
    *, backtest_sharpe: float, backtest_trips: int, shadow_sharpe: float, shadow_fills: int
) -> str:
    """The verdict of one assignment; the rules are in the module docstring."""
    if shadow_fills > 0 and backtest_trips > 0:
        bigger = max(shadow_fills, backtest_trips)
        smaller = min(shadow_fills, backtest_trips)
        if bigger > smaller * COUNT_DIVERGENCE_RATIO:
            return VERDICT_DIVERGES
    if shadow_fills == 0 and backtest_trips == 0:
        return VERDICT_TRACKS
    # sharpes arrive rounded to 3 decimals; rounding the gap keeps the
    # tolerance edges exact (float noise like 1.3 - 1.0 = 0.300...004)
    gap = round(shadow_sharpe - backtest_sharpe, 6)
    if gap < -SHARPE_TOLERANCE:
        return VERDICT_LAGS
    if gap > SHARPE_TOLERANCE:
        return VERDICT_LEADS
    return VERDICT_TRACKS


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
            lines.append(f"{what}: {shadow['note']}")
            continue
        backtest = entry["backtest"]
        lines.append(
            f"{what}: backtest sharpe {backtest['sharpe']} / shadow {shadow['sharpe']}"
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
    return stitch([by_run.get(run.id, []) for run in chain])


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
    cash: float,
    start: datetime,
    end: datetime,
    now: datetime,
    days: int,
) -> dict[str, Any]:
    """The shadow reality of one assignment over the comparison window, or a note."""
    if not chain:
        return {"note": "no shadow runs yet"}
    async with sm_scope(sessionmaker) as session:
        equity = await _chain_equity(session, chain)
        fills = await _chain_window_fills(session, [run.id for run in chain], start, end)
    windowed = slice_window(equity, start, end)
    if len(windowed) < 2:
        assert chain_start is not None  # a non-empty chain always has a start
        age_days = max((now - chain_start).total_seconds() / 86400, 0.0)
        return {"note": f"chain has {age_days:.1f} days so far"}
    metrics = compute_metrics(equity=windowed, fills=fills, timeframe=timeframe, starting_cash=cash)
    overlap_days = (now - start).total_seconds() / 86400
    span_days = (windowed[-1][0] - windowed[0][0]).total_seconds() / 86400
    if not has_min_coverage(overlap_days=overlap_days, span_days=span_days):
        # the metrics stay in the entry; the note suppresses the verdict
        metrics["note"] = f"chain covers {span_days:.1f}d of the {days}d window"
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
    """One report entry: backtest expectation vs shadow reality, plus the verdict."""
    cash = assignment.starting_cash or DEFAULT_STARTING_CASH
    async with sm_scope(sessionmaker) as session:
        chain = await _chain_rows(session, assignment.id)
    chain_start = _chain_start(chain)
    compare_start = compare_window(start, chain_start)
    entry: dict[str, Any] = {
        "id": assignment.id,
        "strategy_id": assignment.strategy_id,
        "pair": assignment.pair,
        "pairs": assignment.pairs,
        "timeframe": assignment.timeframe,
        "start": compare_start.isoformat(),  # the comparison slice both sides ran over
    }
    try:
        if load_error is not None:
            raise ValueError(f"strategies did not load: {load_error}")
        assert strategies is not None
        loaded = strategies.get(assignment.strategy_id)
        if loaded is None:
            raise ValueError(f"unknown strategy {assignment.strategy_id!r}")
        timeframe = Timeframe.parse(assignment.timeframe)
        marker = {"period": period, "assignment": assignment.id, "start": compare_start.isoformat()}
        if assignment.pairs is not None:
            portfolio_request = PortfolioBacktestRequest(
                strategy=loaded,
                params=assignment.params,
                pairs=[Pair.parse(p) for p in assignment.pairs],
                timeframe=timeframe,
                start=compare_start,
                end=end,
                starting_cash=cash,
                rolling_origin=marker,
            )
            run_id, _, metrics = await run_portfolio_backtest(portfolio_request, sessionmaker)
        else:
            request = BacktestRequest(
                strategy=loaded,
                params=assignment.params,
                pair=Pair.parse(assignment.pair),
                timeframe=timeframe,
                start=compare_start,
                end=end,
                starting_cash=cash,
                rolling_origin=marker,
            )
            run_id, _, metrics = await run_backtest(request, sessionmaker)
    except Exception as exc:
        log.warning("rolling-origin backtest failed for assignment %s", assignment.id, exc_info=True)
        return {**entry, "error": str(exc), "verdict": VERDICT_ERROR}
    entry["backtest"] = {"run_id": str(run_id), **metrics}

    shadow = await _shadow_metrics(
        sessionmaker,
        chain=chain,
        chain_start=chain_start,
        timeframe=timeframe,
        cash=cash,
        start=compare_start,
        end=end,
        now=now,
        days=days,
    )
    entry["shadow"] = shadow
    if "note" in shadow:
        # nothing meaningful to compare (new chain, or coverage below the
        # guard floor) — the note suppresses the verdict, even when the
        # short-slice backtest could not produce metrics itself
        entry["verdict"] = VERDICT_UNKNOWN
    elif "error" in metrics:  # the run persisted but had too little data for metrics
        return {**entry, "error": str(metrics["error"]), "verdict": VERDICT_ERROR}
    else:
        entry["verdict"] = verdict_for(
            backtest_sharpe=float(metrics["sharpe"]),
            backtest_trips=int(metrics["num_round_trips"]),
            shadow_sharpe=float(shadow["sharpe"]),
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
