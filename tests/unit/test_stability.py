"""Stability windows: slice math, schema bounds, runner isolation, worker/API wiring.

Runs on a throwaway SQLite file where a session is needed; the backtest
runners are fakes (real execution over canned candles is covered by the
integration tests on Postgres).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import kaupo.backtest.stability as stability_mod
import kaupo.core.backtest_worker as worker_mod
from kaupo.api.routes.backtests import get_backtest
from kaupo.api.schemas import BacktestIn
from kaupo.backtest.run import BacktestRequest
from kaupo.backtest.stability import compute_slices, run_stability_slices, stability_marker
from kaupo.config import get_settings
from kaupo.core.backtest_worker import run_backtest_worker
from kaupo.data.backtest_jobs import STATUS_COMPLETED, enqueue
from kaupo.db.models import BacktestJobRow, Base, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Pair, RunId, Timeframe, utc_now
from kaupo.sdk.loader import load_strategies

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


def _request(**kw: Any) -> BacktestRequest:
    strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
    return BacktestRequest(
        strategy=strategy,
        params={"fast": 5},
        pair=Pair.parse("BTC/EUR"),
        timeframe=Timeframe.H1,
        start=BASE,
        end=BASE + timedelta(hours=48),
        **kw,
    )


class TestComputeSlices:
    def test_exact_non_overlapping_covering(self) -> None:
        slices = compute_slices(BASE, BASE + timedelta(hours=12), 3)
        assert slices == [
            (BASE + timedelta(hours=0), BASE + timedelta(hours=4)),
            (BASE + timedelta(hours=4), BASE + timedelta(hours=8)),
            (BASE + timedelta(hours=8), BASE + timedelta(hours=12)),
        ]

    def test_uneven_span_stays_contiguous_and_ends_at_end(self) -> None:
        start, end = BASE, BASE + timedelta(hours=10)
        slices = compute_slices(start, end, 3)
        assert len(slices) == 3
        assert slices[0][0] == start
        assert slices[-1][1] == end  # no rounding drift
        for (_, prev_end), (next_start, _) in pairwise(slices):
            assert prev_end == next_start  # shared boundaries: no gaps, no overlaps
        assert all(s < e for s, e in slices)

    def test_windows_exceeding_span_raise_clearly(self) -> None:
        # 3 microseconds of span cannot hold 12 windows
        with pytest.raises(ValueError, match="do not fit"):
            compute_slices(BASE, BASE + timedelta(microseconds=3), 12)


class TestMarker:
    def test_slice_marker(self) -> None:
        assert stability_marker("g1", 0, 3) == {"group": "g1", "window": 0, "of": 3}

    def test_full_window_marker(self) -> None:
        assert stability_marker("g1", "full", 3) == {"group": "g1", "window": "full", "of": 3}


class TestSchemaBounds:
    def test_absent_by_default(self) -> None:
        assert BacktestIn(strategy="s", pair="BTC/EUR").stability_windows is None

    def test_bounds_accepted(self) -> None:
        for k in (2, 12):
            body = BacktestIn(strategy="s", pair="BTC/EUR", stability_windows=k)
            assert body.stability_windows == k

    def test_out_of_bounds_rejected(self) -> None:
        for k in (0, 1, 13):
            with pytest.raises(ValidationError):
                BacktestIn(strategy="s", pair="BTC/EUR", stability_windows=k)

    def test_payload_round_trip(self) -> None:
        body = BacktestIn(strategy="s", pair="BTC/EUR", stability_windows=3)
        assert BacktestIn.model_validate(body.model_dump(mode="json")) == body


class TestRunStabilitySlices:
    async def test_slices_run_with_markers_and_forced_persist(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[BacktestRequest] = []

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            seen.append(request)
            return RunId(f"run-{len(seen)}"), None, {"sharpe": 1.0}

        monkeypatch.setattr(stability_mod, "run_backtest", fake_run)
        # persist=False (CLI --no-persist): slices still persist
        result = await run_stability_slices(_request(persist=False), sessionmaker, group="g1", windows=2)

        assert [str(r.pair) for r in seen] == ["BTC/EUR", "BTC/EUR"]  # same config
        assert [r.params for r in seen] == [{"fast": 5}, {"fast": 5}]
        assert [(r.start, r.end) for r in seen] == compute_slices(BASE, BASE + timedelta(hours=48), 2)
        assert [r.persist for r in seen] == [True, True]
        assert [r.stability for r in seen] == [
            {"group": "g1", "window": 0, "of": 2},
            {"group": "g1", "window": 1, "of": 2},
        ]
        assert result["windows"] == 2
        assert [s["run_id"] for s in result["slices"]] == ["run-1", "run-2"]
        assert [s["metrics"] for s in result["slices"]] == [{"sharpe": 1.0}, {"sharpe": 1.0}]
        first, second = result["slices"]
        assert (first["window"], first["start"], first["end"]) == (
            0,
            BASE.isoformat(),
            (BASE + timedelta(hours=24)).isoformat(),
        )
        assert (second["window"], second["end"]) == (1, (BASE + timedelta(hours=48)).isoformat())

    async def test_slice_failure_degrades_to_error_entry(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("No kraken candles for BTC/EUR 1h in range")
            return RunId("run-1"), None, {"sharpe": 1.0}

        monkeypatch.setattr(stability_mod, "run_backtest", fake_run)
        result = await run_stability_slices(_request(), sessionmaker, group="g1", windows=3)

        assert calls == 3  # one failure does not stop the other slices
        ok, bad, ok2 = result["slices"]
        assert ok["run_id"] == "run-1" and "error" not in ok
        assert bad["error"].startswith("ValueError: No kraken candles")
        assert "run_id" not in bad and "metrics" not in bad
        assert bad["window"] == 1  # start/end still recorded
        assert ok2["run_id"] == "run-1"

    async def test_degenerate_span_returns_error_aggregation(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            raise AssertionError("must not run any slice")

        monkeypatch.setattr(stability_mod, "run_backtest", fake_run)
        request = _request()
        object.__setattr__(request, "end", request.start + timedelta(microseconds=3))
        result = await run_stability_slices(request, sessionmaker, group="g1", windows=12)
        assert result["windows"] == 12
        assert result["slices"] == []
        assert "do not fit" in result["error"]


class _FakeResult:
    status = type("S", (), {"value": "completed"})()


class TestWorkerStability:
    async def test_job_completes_with_stored_aggregation(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        aggregation = {
            "windows": 2,
            "slices": [
                {"window": 0, "start": "a", "end": "b", "run_id": "run-0", "metrics": {"sharpe": 1.0}},
                {"window": 1, "start": "b", "end": "c", "run_id": "run-1", "metrics": {"sharpe": 0.5}},
            ],
        }

        async def fake_run_backtest(request: BacktestRequest, sm: Any) -> Any:
            seen["full"] = request
            return RunId("run-full"), _FakeResult(), {"num_fills": 0}

        async def fake_slices(request: BacktestRequest, sm: Any, *, group: str, windows: int) -> Any:
            seen["slices"] = (request, group, windows)
            return aggregation

        monkeypatch.setattr(worker_mod, "run_backtest", fake_run_backtest)
        monkeypatch.setattr(worker_mod, "run_stability_slices", fake_slices)

        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            start=BASE,
            end=BASE + timedelta(hours=48),
            stability_windows=2,
        )
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, body.model_dump(mode="json"))

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_backtest_worker(sessionmaker, get_settings(), stop, poll_interval_seconds=0.05)
        )
        try:
            for _ in range(200):
                async with sm_scope(sessionmaker) as session:
                    row = await session.get(BacktestJobRow, job_id)
                    assert row is not None
                    if row.status != "queued":
                        break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await task

        # the full-window run carries the "full" marker; slices share the job id as group
        assert seen["full"].stability == {"group": job_id, "window": "full", "of": 2}
        assert seen["slices"][1:] == (job_id, 2)
        async with sm_scope(sessionmaker) as session:
            row = await session.get(BacktestJobRow, job_id)
            assert row is not None
            assert row.status == STATUS_COMPLETED
            assert row.run_id == "run-full"
            assert row.result == aggregation

    async def test_no_stability_requested_stores_null_result(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_run_backtest(request: BacktestRequest, sm: Any) -> Any:
            assert request.stability is None  # absent = today's behavior
            return RunId("run-full"), _FakeResult(), {"num_fills": 0}

        async def fake_slices(request: BacktestRequest, sm: Any, *, group: str, windows: int) -> Any:
            raise AssertionError("must not run slices")

        monkeypatch.setattr(worker_mod, "run_backtest", fake_run_backtest)
        monkeypatch.setattr(worker_mod, "run_stability_slices", fake_slices)

        body = BacktestIn(
            strategy="regime-switch", pair="BTC/EUR", start=BASE, end=BASE + timedelta(hours=48)
        )
        async with sm_scope(sessionmaker) as session:
            job_id = await enqueue(session, body.model_dump(mode="json"))

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_backtest_worker(sessionmaker, get_settings(), stop, poll_interval_seconds=0.05)
        )
        try:
            for _ in range(200):
                async with sm_scope(sessionmaker) as session:
                    row = await session.get(BacktestJobRow, job_id)
                    assert row is not None
                    if row.status != "queued":
                        break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await task

        async with sm_scope(sessionmaker) as session:
            row = await session.get(BacktestJobRow, job_id)
            assert row is not None
            assert row.status == STATUS_COMPLETED
            assert row.result is None


class TestGetBacktestShape:
    async def _seed_job(
        self, sessionmaker: async_sessionmaker[AsyncSession], result: dict[str, Any] | None
    ) -> str:
        now = utc_now()
        async with sm_scope(sessionmaker) as session:
            session.add(
                RunRow(
                    id="run-1",
                    mode="backtest",
                    strategy_id="regime-switch",
                    strategy_version="v1",
                    started_at=now,
                    ended_at=now,
                    status="completed",
                    config={},
                    metrics={"num_fills": 0},
                )
            )
            session.add(
                BacktestJobRow(
                    id="job-1",
                    created_at=now,
                    updated_at=now,
                    status=STATUS_COMPLETED,
                    payload={},
                    run_id="run-1",
                    result=result,
                )
            )
        return "job-1"

    async def test_completed_with_stability(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        aggregation = {"windows": 2, "slices": [{"window": 0, "run_id": "r0"}]}
        job_id = await self._seed_job(sessionmaker, aggregation)
        async with sm_scope(sessionmaker) as session:
            body = await get_backtest(None, session, job_id)
        assert body["status"] == "completed"
        assert body["run"]["id"] == "run-1"
        assert body["stability"] == aggregation

    async def test_completed_without_stability(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        job_id = await self._seed_job(sessionmaker, None)
        async with sm_scope(sessionmaker) as session:
            body = await get_backtest(None, session, job_id)
        assert body["status"] == "completed"
        assert body["stability"] is None
