"""Backtest job queue: payload round-trip, claim order, stale sweep, eviction.

Runs on a throwaway SQLite file: the claim's FOR UPDATE SKIP LOCKED is a
no-op there (concurrency semantics are Postgres-only), but the queue logic
— status transitions, ordering, TTL — is dialect-neutral.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.backtest.run import BacktestRequest
from kaupo.config import get_settings
from kaupo.core.backtest_worker import run_backtest_worker
from kaupo.data.backtest_jobs import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    WORKER_STOPPED_ERROR,
    claim_next_queued,
    enqueue,
    evict_finished,
    fail_stale_running,
    mark_completed,
    mark_failed,
)
from kaupo.db.models import BacktestJobRow, Base
from kaupo.db.session import sm_scope

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def sessionmaker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _row(job_id: str, status: str, created_at: datetime, **kw) -> BacktestJobRow:
    return BacktestJobRow(
        id=job_id, created_at=created_at, updated_at=created_at, status=status, payload={}, **kw
    )


async def _get(sessionmaker: async_sessionmaker[AsyncSession], job_id: str) -> BacktestJobRow:
    async with sm_scope(sessionmaker) as session:
        row = await session.get(BacktestJobRow, job_id)
        assert row is not None
        return row


class TestPayloadRoundTrip:
    async def test_single_pair_with_risk_overrides(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            timeframe="1h",
            start=BASE,
            end=BASE + timedelta(hours=48),
            params={"fast": 5},
            max_position_quote=50.0,
        )
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, body.model_dump(mode="json"))

        row = await _get(sessionmaker, job_id)
        assert row.status == STATUS_QUEUED
        assert row.run_id is None and row.error is None
        restored = BacktestIn.model_validate(row.payload)
        assert restored == body

        strategies = lint_and_load_strategies(EXAMPLES_DIR)
        request = build_backtest_request(restored, strategies)
        assert isinstance(request, BacktestRequest)
        assert str(request.pair) == "BTC/EUR"
        assert request.params == {"fast": 5}
        assert request.risk.max_position_quote == 50.0
        assert request.risk.max_gross_exposure_quote == 2000.0  # default kept

    async def test_portfolio(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        body = BacktestIn(
            strategy="momentum-rotation",
            pairs=["BTC/EUR", "SOL/EUR", "ADA/EUR"],
            start=BASE,
            end=BASE + timedelta(hours=48),
            starting_cash=5000.0,
            max_daily_loss_quote=100.0,
        )
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, body.model_dump(mode="json"))

        restored = BacktestIn.model_validate((await _get(sessionmaker, job_id)).payload)
        assert restored == body

        strategies = lint_and_load_strategies(EXAMPLES_DIR)
        request = build_backtest_request(restored, strategies)
        assert isinstance(request, PortfolioBacktestRequest)
        assert [str(p) for p in request.pairs] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]  # canonical order
        assert request.starting_cash == 5000.0
        assert request.risk.max_daily_loss_quote == 100.0


class TestClaim:
    async def test_oldest_queued_first(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            session.add(_row("newest", STATUS_QUEUED, BASE + timedelta(hours=2)))
            session.add(_row("oldest", STATUS_QUEUED, BASE))
            session.add(_row("middle", STATUS_QUEUED, BASE + timedelta(hours=1)))

        claimed: list[str] = []
        for _ in range(3):
            async with sm_scope(sessionmaker) as session:
                row = await claim_next_queued(session)
                assert row is not None
                claimed.append(row.id)
        assert claimed == ["oldest", "middle", "newest"]

        async with sm_scope(sessionmaker) as session:
            assert await claim_next_queued(session) is None  # all running now

    async def test_only_queued_is_claimable(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            session.add(_row("r", STATUS_RUNNING, BASE))
            session.add(_row("c", STATUS_COMPLETED, BASE, run_id="run-1"))
            session.add(_row("f", STATUS_FAILED, BASE, error="boom"))
        async with sm_scope(sessionmaker) as session:
            assert await claim_next_queued(session) is None

    async def test_claim_marks_running(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, {})
        async with sm_scope(sessionmaker) as session:
            row = await claim_next_queued(session)
            assert row is not None and row.status == STATUS_RUNNING
        assert (await _get(sessionmaker, job_id)).status == STATUS_RUNNING


class TestMarkTerminal:
    async def test_completed_sets_run_id(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, {})
        async with sm_scope(sessionmaker) as session:
            await mark_completed(session, job_id, "run-1")
        row = await _get(sessionmaker, job_id)
        assert row.status == STATUS_COMPLETED
        assert row.run_id == "run-1"

    async def test_completed_stores_stability_result(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        aggregation = {"windows": 2, "slices": [{"window": 0, "run_id": "r0", "metrics": {"sharpe": 1.0}}]}
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, {})
        async with sm_scope(sessionmaker) as session:
            await mark_completed(session, job_id, "run-1", aggregation)
        row = await _get(sessionmaker, job_id)
        assert row.status == STATUS_COMPLETED
        assert row.result == aggregation

    async def test_completed_result_null(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, {})
        async with sm_scope(sessionmaker) as session:
            await mark_completed(session, job_id, "run-1")
        assert (await _get(sessionmaker, job_id)).result is None

    async def test_failed_sets_error(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, {})
        async with sm_scope(sessionmaker) as session:
            await mark_failed(session, job_id, "ValueError: no candles")
        row = await _get(sessionmaker, job_id)
        assert row.status == STATUS_FAILED
        assert row.error == "ValueError: no candles"


class TestStaleRunningSweep:
    async def test_running_rows_fail(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        async with sm_scope(sessionmaker) as session:
            session.add(_row("r1", STATUS_RUNNING, BASE))
            session.add(_row("r2", STATUS_RUNNING, BASE))
            session.add(_row("q", STATUS_QUEUED, BASE))
        async with sm_scope(sessionmaker) as session:
            assert await fail_stale_running(session) == 2
        for job_id in ("r1", "r2"):
            row = await _get(sessionmaker, job_id)
            assert row.status == STATUS_FAILED
            assert row.error == WORKER_STOPPED_ERROR
        assert (await _get(sessionmaker, "q")).status == STATUS_QUEUED  # untouched


class TestEviction:
    async def test_old_finished_rows_deleted(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(hours=25)
        async with sm_scope(sessionmaker) as session:
            session.add(_row("old-done", STATUS_COMPLETED, old, run_id="run-1"))
            session.add(_row("old-failed", STATUS_FAILED, old, error="boom"))
            session.add(_row("new-done", STATUS_COMPLETED, now - timedelta(hours=1), run_id="run-2"))
            session.add(_row("old-queued", STATUS_QUEUED, old))  # unfinished: never evicted
            session.add(_row("old-running", STATUS_RUNNING, old))
        async with sm_scope(sessionmaker) as session:
            assert await evict_finished(session) == 2
        async with sm_scope(sessionmaker) as session:
            from sqlalchemy import select

            remaining = set((await session.execute(select(BacktestJobRow.id))).scalars().all())
        assert remaining == {"new-done", "old-queued", "old-running"}


class TestWorkerLoop:
    async def test_build_failure_marks_job_failed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        # the payload names a strategy that no longer exists: the job must
        # fail with the real reason, in the preserved error format
        payload = BacktestIn(strategy="nope", pair="BTC/EUR").model_dump(mode="json")
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, payload)

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_backtest_worker(sessionmaker, get_settings(), stop, poll_interval_seconds=0.05)
        )
        try:
            for _ in range(200):
                row = await _get(sessionmaker, job_id)
                if row.status != STATUS_QUEUED:
                    break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await task

        row = await _get(sessionmaker, job_id)
        assert row.status == STATUS_FAILED
        assert row.error is not None
        assert row.error.startswith("UnknownStrategyError: unknown strategy 'nope'; available: [")
        assert "'regime-switch'" in row.error
