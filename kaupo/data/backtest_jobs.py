"""Backtest job queue: durable hand-off from the API to the backtest worker.

The API inserts one ``queued`` row per submitted backtest; a worker claims
the oldest queued row (``FOR UPDATE SKIP LOCKED``, so concurrent workers
never take the same job), runs it, and marks it ``completed`` with the runs
row id or ``failed`` with the error. Rows survive process restarts, so a
job is never lost — the worker fails ``running`` rows left behind by a
crashed worker at startup, and evicts finished rows after ``JOB_TTL``.
"""

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import BacktestJobRow
from kaupo.domain import new_id, utc_now

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

JOB_TTL = timedelta(hours=24)
WORKER_STOPPED_ERROR = "worker stopped before completion"


async def enqueue(session: AsyncSession, payload: dict[str, Any]) -> str:
    """Insert a queued job for the validated request body; return the job id."""
    job_id = new_id()
    now = utc_now()
    session.add(
        BacktestJobRow(
            id=job_id,
            created_at=now,
            updated_at=now,
            status=STATUS_QUEUED,
            payload=payload,
        )
    )
    await session.flush()
    return job_id


async def claim_next_queued(session: AsyncSession) -> BacktestJobRow | None:
    """Mark the oldest queued job running and return it, or None when the queue is empty.

    Skip-locked, so a concurrent worker claims the next row instead of
    blocking; the caller must commit to release the row lock.
    """
    row = (
        (
            await session.execute(
                select(BacktestJobRow)
                .where(BacktestJobRow.status == STATUS_QUEUED)
                .order_by(BacktestJobRow.created_at, BacktestJobRow.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    row.status = STATUS_RUNNING
    row.updated_at = utc_now()
    return row


async def mark_completed(
    session: AsyncSession, job_id: str, run_id: str, result: dict[str, Any] | None = None
) -> None:
    row = await session.get(BacktestJobRow, job_id)
    assert row is not None  # the worker claimed it
    row.status = STATUS_COMPLETED
    row.run_id = run_id
    row.result = result
    row.updated_at = utc_now()


async def mark_failed(session: AsyncSession, job_id: str, error: str) -> None:
    row = await session.get(BacktestJobRow, job_id)
    assert row is not None  # the worker claimed it
    row.status = STATUS_FAILED
    row.error = error
    row.updated_at = utc_now()


async def fail_stale_running(session: AsyncSession, error: str = WORKER_STOPPED_ERROR) -> int:
    """Fail jobs left 'running' by a worker that died mid-job; returns the count."""
    rows = (
        (await session.execute(select(BacktestJobRow).where(BacktestJobRow.status == STATUS_RUNNING)))
        .scalars()
        .all()
    )
    for row in rows:
        row.status = STATUS_FAILED
        row.error = error
        row.updated_at = utc_now()
    return len(rows)


async def evict_finished(session: AsyncSession, ttl: timedelta = JOB_TTL) -> int:
    """Delete completed/failed rows older than the TTL; returns the count."""
    cutoff = utc_now() - ttl
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(BacktestJobRow).where(
                BacktestJobRow.status.in_((STATUS_COMPLETED, STATUS_FAILED)),
                BacktestJobRow.created_at < cutoff,
            )
        ),
    )
    return result.rowcount
