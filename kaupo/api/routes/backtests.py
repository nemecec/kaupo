"""Backtest jobs: submit async, poll for results."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import BacktestAccepted, BacktestIn, RunOut
from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, backtest_risk_config, run_backtest
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
        self.created_at = datetime.now(UTC)
        self.run_id: RunId | None = None
        self.error: str | None = None


# in-memory job registry (single-process API); evicted after 24h
_jobs: dict[str, BacktestJob] = {}
_JOB_TTL = timedelta(hours=24)


def _evict_old_jobs() -> None:
    cutoff = datetime.now(UTC) - _JOB_TTL
    stale = [jid for jid, job in _jobs.items() if job.created_at < cutoff]
    for jid in stale:
        job = _jobs.pop(jid)
        if not job.task.done():
            job.task.cancel()


async def _execute(job_id: str, request: BacktestRequest | PortfolioBacktestRequest) -> None:
    job = _jobs[job_id]
    try:
        if isinstance(request, PortfolioBacktestRequest):
            run_id, _, _ = await run_portfolio_backtest(request, get_sessionmaker())
        else:
            run_id, _, _ = await run_backtest(request, get_sessionmaker())
        job.run_id = run_id
    except Exception as exc:
        log.exception("Backtest job %s failed", job_id)
        job.error = f"{type(exc).__name__}: {exc}"[:300]


@router.post("", status_code=202)
async def submit_backtest(
    body: BacktestIn,
    _: Annotated[Principal, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BacktestAccepted:
    from kaupo.sdk.lint import lint_directory

    try:
        violations = lint_directory(settings.strategies_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"strategies dir misconfigured: {exc}") from exc
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
    loaded = strategies[body.strategy]

    def aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    end = aware(body.end) if body.end else datetime.now(UTC)
    start = aware(body.start) if body.start else end - timedelta(days=body.days)

    request: BacktestRequest | PortfolioBacktestRequest
    try:
        risk = backtest_risk_config(
            max_position_quote=body.max_position_quote,
            max_gross_exposure_quote=body.max_gross_exposure_quote,
            max_daily_loss_quote=body.max_daily_loss_quote,
        )
        timeframe = Timeframe.parse(body.timeframe)
        if body.pairs is not None:
            if not loaded.is_portfolio:
                raise HTTPException(
                    status_code=422,
                    detail=f"strategy {body.strategy!r} is not a portfolio strategy; pass pair",
                )
            request = PortfolioBacktestRequest(
                strategy=loaded,
                params=body.params,
                pairs=[Pair.parse(p) for p in body.pairs],
                timeframe=timeframe,
                start=start,
                end=end,
                starting_cash=body.starting_cash,
                exchange=body.exchange,
                risk=risk,
            )
        else:
            if loaded.is_portfolio:
                raise HTTPException(
                    status_code=422,
                    detail=f"strategy {body.strategy!r} is a portfolio strategy; pass pairs",
                )
            assert body.pair is not None  # the schema guarantees exactly one of pair/pairs
            request = BacktestRequest(
                strategy=loaded,
                params=body.params,
                pair=Pair.parse(body.pair),
                timeframe=timeframe,
                start=start,
                end=end,
                starting_cash=body.starting_cash,
                exchange=body.exchange,
                risk=risk,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from kaupo.domain import new_id

    _evict_old_jobs()
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
    _evict_old_jobs()
    job = _jobs.get(job_id)
    if job is None:
        # maybe it's a run id from a finished/older job (e.g. after restart)
        row = await session.get(RunRow, job_id)
        if row is not None and row.mode == "backtest":
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
