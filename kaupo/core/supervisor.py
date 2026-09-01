"""Desired-state supervisor: reconciles live shadow runs to run_assignments rows.

Enabled rows are the desired state. Each runs as an in-process asyncio task
(``run_shadow``, or ``run_portfolio_shadow`` for a row with a ``pairs``
universe, with its own stop event and its own Kraken client). The poll
loop diffs desired rows against the live tasks: it starts what is missing,
stops what is disabled, deleted, or config-changed, and restarts crashes
after a backoff. A run killed through the control channel stays down until a
resume command targets it or its assignment row is updated.
"""

import asyncio
import enum
import logging
import traceback
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import RunResult
from kaupo.core.resume import config_hash as config_hash
from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest, run_portfolio_shadow, run_shadow
from kaupo.data.assignments import Assignment, list_assignments
from kaupo.data.binance import BinanceClient
from kaupo.data.kraken import KrakenClient
from kaupo.db.models import EquitySnapshotRow, EventRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Pair, RunMode, RunStatus, Timeframe, utc_now
from kaupo.sdk.protocol import LoadedStrategy

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 15.0
RESTART_BACKOFF = timedelta(seconds=60)
DEFAULT_STARTING_CASH = 10_000.0

# Watchdog: a run whose newest equity snapshot is older than this is stalled.
# The snapshot ts is the candle OPEN time: with per-candle flushing it lands
# at close + seconds, so a healthy run's newest ts oscillates between 1x and
# 2x the timeframe behind wall clock. The threshold must exceed 2x — at 1.5x
# the watchdog cancelled a healthy 1h run 20 minutes before its next tick
# (2026-09-01 03:40 UTC false positive).
WATCHDOG_STALE_MULTIPLIER = 2.0
WATCHDOG_GRACE = timedelta(minutes=10)


class EndKind(enum.Enum):
    """How a finished run task ended, and what the supervisor may do about it."""

    STOPPED = "stopped"  # the supervisor set its stop event: nothing to do
    KILLED = "killed"  # control-channel kill: stays down until resume/update
    RESTART = "restart"  # control-channel switch: deliberate graceful restart
    CRASHED = "crashed"  # failure or engine halt: restart after the backoff


def classify_end(stop_requested: bool, failed: bool, latest_command: str | None) -> EndKind:
    """Classify a finished task: its stop event, an exception, else the control channel."""
    if stop_requested:
        return EndKind.STOPPED
    if failed:
        return EndKind.CRASHED
    if latest_command == "kill":
        return EndKind.KILLED
    if latest_command == "switch":
        return EndKind.RESTART
    return EndKind.CRASHED


def in_backoff(failed_at: datetime, now: datetime, backoff: timedelta = RESTART_BACKOFF) -> bool:
    """True while the crash-restart backoff is still running."""
    return now - failed_at < backoff


def resume_cleared(observed_at: datetime, updated_at: datetime, latest_command: str | None) -> bool:
    """A control-killed run resumes on a resume command or an assignment update."""
    return latest_command == "resume" or updated_at > observed_at


def watchdog_stale_after(timeframe: Timeframe, grace: timedelta = WATCHDOG_GRACE) -> timedelta:
    """How long a run may go without a fresh equity snapshot before it is stalled."""
    return timedelta(seconds=WATCHDOG_STALE_MULTIPLIER * timeframe.seconds) + grace


def watchdog_is_stale(
    reference: datetime, now: datetime, timeframe: Timeframe, grace: timedelta = WATCHDOG_GRACE
) -> bool:
    """``reference`` is the last sign of progress: max(run start, latest snapshot ts)."""
    return now - reference > watchdog_stale_after(timeframe, grace)


@dataclass(frozen=True)
class ReconcilePlan:
    start: list[str]  # assignment ids to start
    stop: list[str]  # assignment ids to stop gracefully


def reconcile(desired: dict[str, str], live: dict[str, str], held_down: set[str]) -> ReconcilePlan:
    """Diff desired rows (id → config hash) against live tasks (id → config hash).

    A hash change appears in ``stop`` only: the old task stops first, and the
    start follows on the next pass, once the task is gone. Held-down rows
    (control-killed, crash backoff) are not started.
    """
    stop = [aid for aid, live_hash in live.items() if aid not in desired or desired[aid] != live_hash]
    start = [aid for aid in desired if aid not in live and aid not in held_down]
    return ReconcilePlan(start=start, stop=stop)


@dataclass
class _LiveRun:
    config_hash: str
    stop: asyncio.Event
    task: asyncio.Task[RunResult]


@dataclass(frozen=True)
class _KilledRun:
    run_id: str
    started_at: datetime
    observed_at: datetime  # when the supervisor saw the kill


async def _latest_run_row(session: AsyncSession, assignment_id: str) -> RunRow | None:
    """The newest run started for an assignment (the run config carries its row id)."""
    return (
        (
            await session.execute(
                select(RunRow)
                .where(RunRow.config["assignment_id"].as_string() == assignment_id)
                .order_by(RunRow.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_control_command(session: AsyncSession, run_id: str, not_before: datetime) -> str | None:
    """Latest control command for the run (or all runs); mirrors DbControlProbe."""
    run_id_col = EventRow.data["run_id"].as_string()
    row = (
        (
            await session.execute(
                select(EventRow)
                .where(
                    EventRow.source == "control",
                    EventRow.ts >= not_before,
                    (run_id_col.is_(None)) | (run_id_col == run_id),
                )
                .order_by(EventRow.ts.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    command = (row.data or {}).get("command")
    return command if isinstance(command, str) else None


async def halt_orphan_runs(session: AsyncSession) -> int:
    """Halt shadow-mode 'running' rows that match no enabled assignment.

    Such rows belong to dead processes (an old shadow container, a crashed
    supervisor) — the same slot-claiming idea as DbRecorder.start, which
    supersedes stale rows of the same strategy, pair, and timeframe.
    Matching is mode + strategy + config pair + config timeframe.
    """
    enabled = [
        a for a in await list_assignments(session, enabled_only=True) if a.mode == RunMode.SHADOW.value
    ]
    slots = {(a.strategy_id, a.pair, a.timeframe) for a in enabled}
    rows = (
        (
            await session.execute(
                select(RunRow).where(
                    RunRow.mode == RunMode.SHADOW.value,
                    RunRow.status == RunStatus.RUNNING.value,
                )
            )
        )
        .scalars()
        .all()
    )
    halted = 0
    for row in rows:
        config = row.config or {}
        if (row.strategy_id, config.get("pair"), config.get("timeframe")) in slots:
            continue
        row.status = RunStatus.HALTED.value
        row.ended_at = utc_now()
        row.metrics = {"halt_reason": "no matching assignment"}
        halted += 1
    return halted


async def _run_assignment(
    assignment: Assignment,
    strategies: dict[str, LoadedStrategy],
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    poll_interval_seconds: float,
    funding_refresh_seconds: float,
) -> RunResult:
    strategy = strategies[assignment.strategy_id]  # guarded by the supervisor's start check
    # One Kraken client per run: each run gets its own exchange rate-limit
    # bucket. A shared client may be wanted later, when runs multiply.
    async with KrakenClient() as client, BinanceClient() as funding_client:
        if assignment.pairs is not None:
            portfolio_request = PortfolioShadowRequest(
                strategy=strategy,
                params=assignment.params,
                pairs=[Pair.parse(p) for p in assignment.pairs],
                timeframe=Timeframe.parse(assignment.timeframe),
                starting_cash=assignment.starting_cash or DEFAULT_STARTING_CASH,
                poll_interval_seconds=poll_interval_seconds,
                assignment_id=assignment.id,
                funding_refresh_seconds=funding_refresh_seconds,
            )
            return await run_portfolio_shadow(
                portfolio_request, sessionmaker, client, stop, funding_client=funding_client
            )
        request = ShadowRequest(
            strategy=strategy,
            params=assignment.params,
            pair=Pair.parse(assignment.pair),
            timeframe=Timeframe.parse(assignment.timeframe),
            starting_cash=assignment.starting_cash or DEFAULT_STARTING_CASH,
            poll_interval_seconds=poll_interval_seconds,
            assignment_id=assignment.id,
            funding_refresh_seconds=funding_refresh_seconds,
        )
        return await run_shadow(request, sessionmaker, client, stop, funding_client=funding_client)


def _log_task_stack(aid: str, task: asyncio.Task[RunResult]) -> None:
    """Log where the stalled task is suspended — the root-cause clue for next time."""
    frames = task.get_stack()
    if not frames:
        log.warning("Watchdog: assignment %s task shows no stack (already done?)", aid)
        return
    for frame in frames:
        log.warning("Watchdog: assignment %s suspended at:\n%s", aid, "".join(traceback.format_stack(frame)))


async def _stalled_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    live: dict[str, _LiveRun],
    assignments: Mapping[str, Assignment],
) -> list[str]:
    """Live assignment ids whose run stopped writing equity snapshots.

    A stalled run looks healthy everywhere else: the task is alive, no
    exception, run row 'running' (the 2026-08-31 silent stall, kaupo#31).
    """
    stalled: list[str] = []
    now = utc_now()
    async with sm_scope(sessionmaker) as session:
        for aid in live:
            assignment = assignments.get(aid)
            if assignment is None:
                continue  # not desired anymore; reconcile stops it this pass
            row = await _latest_run_row(session, aid)
            if row is None:
                continue  # starting up: no run row yet, give it the reconcile cycle
            last_ts = (
                await session.execute(
                    select(func.max(EquitySnapshotRow.ts)).where(EquitySnapshotRow.run_id == row.id)
                )
            ).scalar_one_or_none()
            reference = max(row.started_at, last_ts) if last_ts is not None else row.started_at
            if watchdog_is_stale(reference, now, Timeframe.parse(assignment.timeframe)):
                stalled.append(aid)
    return stalled


async def _reap_finished(
    sessionmaker: async_sessionmaker[AsyncSession],
    live: dict[str, _LiveRun],
    killed: dict[str, _KilledRun],
    backoff: dict[str, datetime],
    restart_backoff: timedelta,
) -> None:
    """Collect done tasks and decide each one's fate (see classify_end)."""
    for aid, lr in list(live.items()):
        if not lr.task.done():
            continue
        del live[aid]
        exc: BaseException | None = None
        if not lr.task.cancelled():
            exc = lr.task.exception()
        if exc is not None:
            log.error("Run for assignment %s crashed", aid, exc_info=exc)
        row: RunRow | None = None
        command: str | None = None
        if exc is None and not lr.stop.is_set():
            async with sm_scope(sessionmaker) as session:
                row = await _latest_run_row(session, aid)
                if row is not None:
                    command = await _latest_control_command(session, row.id, row.started_at)
        kind = classify_end(lr.stop.is_set(), exc is not None, command)
        if kind is EndKind.STOPPED:
            continue
        if kind is EndKind.KILLED and row is not None:
            killed[aid] = _KilledRun(run_id=row.id, started_at=row.started_at, observed_at=utc_now())
            log.warning(
                "Run %s for assignment %s killed via control; down until resume or update",
                row.id,
                aid,
            )
        elif kind is EndKind.RESTART:
            log.info("Run for assignment %s restarted via control switch", aid)
        else:  # CRASHED (or a kill whose run row is already gone)
            backoff[aid] = utc_now()
            log.warning(
                "Run for assignment %s ended unexpectedly; restart backed off %ds",
                aid,
                int(restart_backoff.total_seconds()),
            )


async def _refresh_killed(
    sessionmaker: async_sessionmaker[AsyncSession],
    rows: list[Assignment],
    killed: dict[str, _KilledRun],
) -> None:
    """Release held-down assignments on a resume command or a row update."""
    enabled = {a.id: a for a in rows}
    for aid, k in list(killed.items()):
        row = enabled.get(aid)
        if row is None:
            del killed[aid]  # disabled or deleted: nothing to resume
            continue
        async with sm_scope(sessionmaker) as session:
            command = await _latest_control_command(session, k.run_id, k.started_at)
        if resume_cleared(k.observed_at, row.updated_at, command):
            log.info("Resuming assignment %s (control resume or row update)", aid)
            del killed[aid]


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_supervisor(
    sessionmaker: async_sessionmaker[AsyncSession],
    strategies: dict[str, LoadedStrategy],
    stop: asyncio.Event,
    *,
    reconcile_interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
    restart_backoff: timedelta = RESTART_BACKOFF,
    run_poll_interval_seconds: float = 20.0,
    run_funding_refresh_seconds: float = 1800.0,
) -> None:
    """Reconcile live shadow runs to the enabled assignments until ``stop`` is set."""
    live: dict[str, _LiveRun] = {}
    killed: dict[str, _KilledRun] = {}
    backoff: dict[str, datetime] = {}
    async with sm_scope(sessionmaker) as session:
        halted = await halt_orphan_runs(session)
    if halted:
        log.info("Halted %d orphan shadow run row(s): no matching assignment", halted)
    log.info("Supervisor started (reconcile every %.0fs)", reconcile_interval_seconds)
    try:
        while not stop.is_set():
            await _reap_finished(sessionmaker, live, killed, backoff, restart_backoff)
            async with sm_scope(sessionmaker) as session:
                rows = await list_assignments(session, enabled_only=True)
            rows = [a for a in rows if a.mode == RunMode.SHADOW.value]
            by_id = {a.id: a for a in rows}
            for aid in await _stalled_runs(sessionmaker, live, by_id):
                lr = live.pop(aid)
                _log_task_stack(aid, lr.task)
                log.warning("Watchdog: run for assignment %s stalled; cancelling for restart", aid)
                lr.task.cancel()
                # restart through the usual crash backoff; if the task ignores
                # cancellation it stays wedged but silent — the replacement
                # supersedes its run row when it starts
                backoff[aid] = utc_now()
            await _refresh_killed(sessionmaker, rows, killed)
            held_down = set(killed) | {
                aid for aid, failed_at in backoff.items() if in_backoff(failed_at, utc_now(), restart_backoff)
            }
            desired = {a.id: config_hash(a.strategy_id, a.pair, a.timeframe, a.params, a.pairs) for a in rows}
            plan = reconcile(desired, {aid: lr.config_hash for aid, lr in live.items()}, held_down)
            for aid in plan.stop:
                log.info("Stopping run for assignment %s (disabled, deleted, or changed)", aid)
                live[aid].stop.set()
            for aid in plan.start:
                assignment = by_id[aid]
                loaded = strategies.get(assignment.strategy_id)
                if loaded is None:
                    log.error(
                        "Assignment %s: unknown strategy %r; not starting",
                        aid,
                        assignment.strategy_id,
                    )
                    backoff[aid] = utc_now()  # no hot retry loop
                    continue
                if (assignment.pairs is not None) != loaded.is_portfolio:
                    log.error(
                        "Assignment %s: strategy %r (portfolio=%s) does not match the assignment "
                        "(pairs=%s); not starting",
                        aid,
                        assignment.strategy_id,
                        loaded.is_portfolio,
                        assignment.pairs is not None,
                    )
                    backoff[aid] = utc_now()  # no hot retry loop
                    continue
                backoff.pop(aid, None)
                stop_event = asyncio.Event()
                task = asyncio.create_task(
                    _run_assignment(
                        assignment,
                        strategies,
                        sessionmaker,
                        stop_event,
                        run_poll_interval_seconds,
                        run_funding_refresh_seconds,
                    ),
                    name=f"assignment-{aid}",
                )
                live[aid] = _LiveRun(config_hash=desired[aid], stop=stop_event, task=task)
                log.info(
                    "Started run for assignment %s: %s on %s %s",
                    aid,
                    assignment.strategy_id,
                    assignment.pair,
                    assignment.timeframe,
                )
            await _sleep(stop, reconcile_interval_seconds)
    finally:
        for lr in live.values():
            lr.stop.set()
        if live:
            await asyncio.gather(*(lr.task for lr in live.values()), return_exceptions=True)
