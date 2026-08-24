"""Backtest jobs: submit async, poll for results."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import BacktestAccepted, BacktestIn, RunOut
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.config import Settings, get_settings
from kaupo.db.models import RunRow
from kaupo.db.session import get_session, get_sessionmaker
from kaupo.domain import Pair, RunId, Timeframe
from kaupo.sdk.loader import load_strategies

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])


class BacktestJob:
    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        self.run_id: RunId | None = None
        self.error: str | None = None


# in-memory job registry (single-process API)
_jobs: dict[str, BacktestJob] = {}


async def _execute(job_id: str, request: BacktestRequest) -> None:
    job = _jobs[job_id]
    try:
        run_id, _, _ = await run_backtest(request, get_sessionmaker())
        job.run_id = run_id
    except Exception as exc:
        log.exception("Backtest job %s failed", job_id)
        job.error = str(exc)


@router.post("", status_code=202)
async def submit_backtest(
    body: BacktestIn,
    _: Annotated[Principal, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BacktestAccepted:
    from kaupo.sdk.lint import lint_directory

    violations = lint_directory(settings.strategies_dir)
    if violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "strategies have determinism violations",
                "violations": [str(v) for v in violations],
            },
        )
    strategies = load_strategies(settings.strategies_dir)
    if body.strategy not in strategies:
        raise HTTPException(
            status_code=404,
            detail=f"unknown strategy {body.strategy!r}; available: {sorted(strategies)}",
        )
    end = body.end or datetime.now(UTC)
    start = body.start or end - timedelta(days=body.days)
    request = BacktestRequest(
        strategy=strategies[body.strategy],
        params=body.params,
        pair=Pair.parse(body.pair),
        timeframe=Timeframe.parse(body.timeframe),
        start=start,
        end=end,
        starting_cash=body.starting_cash,
    )
    from kaupo.domain import new_id

    job_id = new_id()
    task = asyncio.create_task(_execute(job_id, request))
    _jobs[job_id] = BacktestJob(task)
    return BacktestAccepted(run_id=job_id)


@router.get("/{job_id}")
async def get_backtest(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    job_id: str,
) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"backtest job {job_id} not found")
    if not job.task.done():
        return {"job_id": job_id, "status": "running"}
    if job.error:
        return {"job_id": job_id, "status": "failed", "error": job.error}

    row = await session.get(RunRow, job.run_id)
    if row is None:
        return {"job_id": job_id, "status": "failed", "error": "run row missing"}
    return {
        "job_id": job_id,
        "status": "completed",
        "run": RunOut(
            id=row.id,
            mode=row.mode,
            strategy_id=row.strategy_id,
            strategy_version=row.strategy_version,
            started_at=row.started_at,
            ended_at=row.ended_at,
            status=row.status,
            config=row.config,
            metrics=row.metrics,
        ).model_dump(mode="json"),
    }
