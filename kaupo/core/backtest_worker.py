"""Backtest worker: executes queued backtest_jobs rows, one at a time.

A dedicated process (`kaupo run backtest-worker`) so heavy portfolio
backtests never compete with the API or the supervisor. The loop claims
the oldest queued job (skip-locked), rebuilds the request from the stored
payload through the same validation path as the API, executes it, and
marks the job completed with the runs row id or failed with the error.
On startup, jobs left 'running' by a dead worker are failed; finished
rows older than the TTL are evicted once per loop.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import run_backtest
from kaupo.backtest.stability import run_stability_slices, stability_marker
from kaupo.config import Settings
from kaupo.data import backtest_jobs
from kaupo.data.backtest_jobs import JOB_TTL
from kaupo.db.models import BacktestJobRow
from kaupo.db.session import sm_scope

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0


async def _claim_next(sessionmaker: async_sessionmaker[AsyncSession]) -> BacktestJobRow | None:
    async with sm_scope(sessionmaker) as session:
        return await backtest_jobs.claim_next_queued(session)


async def _execute(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job: BacktestJobRow,
) -> None:
    try:
        body = BacktestIn.model_validate(job.payload)
        strategies = lint_and_load_strategies(settings.strategies_dir)
        request = build_backtest_request(body, strategies)
        if body.stability_windows is not None:
            request = replace(request, stability=stability_marker(job.id, "full", body.stability_windows))
        if isinstance(request, PortfolioBacktestRequest):
            run_id, _, _ = await run_portfolio_backtest(request, sessionmaker)
        else:
            run_id, _, _ = await run_backtest(request, sessionmaker)
        # stability windows run after the full-window run; a slice failure
        # degrades to an error entry and never fails the job
        result = None
        if body.stability_windows is not None:
            result = await run_stability_slices(
                request, sessionmaker, group=job.id, windows=body.stability_windows
            )
    except Exception as exc:
        log.exception("Backtest job %s failed", job.id)
        error = f"{type(exc).__name__}: {exc}"[:300]
        async with sm_scope(sessionmaker) as session:
            await backtest_jobs.mark_failed(session, job.id, error)
        return
    async with sm_scope(sessionmaker) as session:
        await backtest_jobs.mark_completed(session, job.id, run_id, result)
    log.info("Backtest job %s completed: run %s", job.id, run_id)


async def run_backtest_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    *,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    job_ttl: timedelta = JOB_TTL,
) -> None:
    """Execute queued backtest jobs until ``stop`` is set; the current job finishes first."""
    async with sm_scope(sessionmaker) as session:
        swept = await backtest_jobs.fail_stale_running(session)
    if swept:
        log.warning("Failed %d job(s) left running by a dead worker", swept)
    log.info("Backtest worker started (poll every %.1fs)", poll_interval_seconds)
    while not stop.is_set():
        async with sm_scope(sessionmaker) as session:
            evicted = await backtest_jobs.evict_finished(session, job_ttl)
        if evicted:
            log.info("Evicted %d finished backtest job(s) older than %s", evicted, job_ttl)
        job = await _claim_next(sessionmaker)
        if job is None:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
            continue
        await _execute(sessionmaker, settings, job)
    log.info("Backtest worker stopped")
