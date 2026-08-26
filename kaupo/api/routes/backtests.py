"""Backtest jobs: submit to the durable queue, poll for results.

The API never executes backtests. POST validates the body (lint gate,
strategy lookup, request construction — the same code path the worker
uses) and enqueues a job row; a backtest worker process claims and
executes it. Jobs survive an API restart and simply wait queued while no
worker runs.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import BacktestAccepted, BacktestIn, RunOut
from kaupo.backtest.plan import (
    LintViolationsError,
    UnknownStrategyError,
    build_backtest_request,
    lint_and_load_strategies,
)
from kaupo.config import Settings, get_settings
from kaupo.data import backtest_jobs
from kaupo.db.models import BacktestJobRow, RunRow
from kaupo.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])


@router.post("", status_code=202)
async def submit_backtest(
    body: BacktestIn,
    _: Annotated[Principal, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BacktestAccepted:
    try:
        strategies = lint_and_load_strategies(settings.strategies_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"strategies dir misconfigured: {exc}") from exc
    except LintViolationsError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "strategies have determinism violations",
                "violations": exc.violations,
            },
        ) from exc
    try:
        build_backtest_request(body, strategies)
    except UnknownStrategyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job_id = await backtest_jobs.enqueue(session, body.model_dump(mode="json"))
    return BacktestAccepted(run_id=job_id)


@router.get("/{job_id}")
async def get_backtest(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    job_id: str,
) -> dict[str, Any]:
    job = await session.get(BacktestJobRow, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"backtest job {job_id} not found")
    if job.status in (backtest_jobs.STATUS_QUEUED, backtest_jobs.STATUS_RUNNING):
        return {"job_id": job_id, "status": "running"}
    if job.status == backtest_jobs.STATUS_FAILED:
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
        # the stability-window aggregation; null when none was requested
        "stability": job.result,
    }
