"""Desired-state supervisor: reconciles live shadow runs to run_assignments rows.

Enabled rows are the desired state. Each runs as an in-process asyncio task
(``run_shadow`` with its own stop event and its own Kraken client). The poll
loop diffs desired rows against the live tasks: it starts what is missing,
stops what is disabled, deleted, or config-changed, and restarts crashes
after a backoff. A run killed through the control channel stays down until a
resume command targets it or its assignment row is updated.
"""

import asyncio
import enum
import hashlib
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import RunResult
from kaupo.core.runner import ShadowRequest, run_shadow
from kaupo.data.assignments import Assignment, list_assignments
from kaupo.data.kraken import KrakenClient
from kaupo.db.models import EventRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Pair, RunMode, RunStatus, Timeframe, utc_now
from kaupo.sdk.protocol import LoadedStrategy

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 15.0
RESTART_BACKOFF = timedelta(seconds=60)
DEFAULT_STARTING_CASH = 10_000.0


def config_hash(strategy_id: str, pair: str, timeframe: str, params: dict[str, Any]) -> str:
    """Stable hash of the run-defining fields of an assignment.

    A change in strategy, pair, timeframe, or params restarts the run;
    anything else (enabled flag, starting cash) does not.
    """
    canonical = json.dumps(
        {"strategy": strategy_id, "pair": pair, "timeframe": timeframe, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    supersedes stale rows of the same strategy and pair. Matching is
    mode + strategy + config pair.
    """
    enabled = [
        a for a in await list_assignments(session, enabled_only=True) if a.mode == RunMode.SHADOW.value
    ]
    slots = {(a.strategy_id, a.pair) for a in enabled}
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
        if (row.strategy_id, (row.config or {}).get("pair")) in slots:
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
) -> RunResult:
    strategy = strategies[assignment.strategy_id]  # guarded by the supervisor's start check
    # One Kraken client per run: each run gets its own exchange rate-limit
    # bucket. A shared client may be wanted later, when runs multiply.
    async with KrakenClient() as client:
        request = ShadowRequest(
            strategy=strategy,
            params=assignment.params,
            pair=Pair.parse(assignment.pair),
            timeframe=Timeframe.parse(assignment.timeframe),
            starting_cash=assignment.starting_cash or DEFAULT_STARTING_CASH,
            poll_interval_seconds=poll_interval_seconds,
            assignment_id=assignment.id,
        )
        return await run_shadow(request, sessionmaker, client, stop)


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
            await _refresh_killed(sessionmaker, rows, killed)
            held_down = set(killed) | {
                aid for aid, failed_at in backoff.items() if in_backoff(failed_at, utc_now(), restart_backoff)
            }
            desired = {a.id: config_hash(a.strategy_id, a.pair, a.timeframe, a.params) for a in rows}
            plan = reconcile(desired, {aid: lr.config_hash for aid, lr in live.items()}, held_down)
            for aid in plan.stop:
                log.info("Stopping run for assignment %s (disabled, deleted, or changed)", aid)
                live[aid].stop.set()
            for aid in plan.start:
                assignment = by_id[aid]
                if assignment.strategy_id not in strategies:
                    log.error(
                        "Assignment %s: unknown strategy %r; not starting",
                        aid,
                        assignment.strategy_id,
                    )
                    backoff[aid] = utc_now()  # no hot retry loop
                    continue
                backoff.pop(aid, None)
                stop_event = asyncio.Event()
                task = asyncio.create_task(
                    _run_assignment(
                        assignment, strategies, sessionmaker, stop_event, run_poll_interval_seconds
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
