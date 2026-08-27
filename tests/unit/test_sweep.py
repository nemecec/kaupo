"""Parameter sweeps: grid math, spec validation, runner isolation, worker/API wiring.

Runs on a throwaway SQLite file where a session is needed; the backtest
runners are fakes (real execution over canned candles is covered by the
integration tests on Postgres).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import kaupo.backtest.sweep as sweep_mod
import kaupo.core.backtest_worker as worker_mod
from kaupo.api.routes.backtests import get_backtest
from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.run import BacktestRequest
from kaupo.backtest.sweep import (
    MAX_SWEEP_POINTS,
    expand_sweep,
    run_sweep,
    sweep_marker,
    sweep_size,
    validate_sweep_keys,
    validate_sweep_spec,
)
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
        params={"adx_period": 7},
        pair=Pair.parse("BTC/EUR"),
        timeframe=Timeframe.H1,
        start=BASE,
        end=BASE + timedelta(hours=48),
        **kw,
    )


class TestExpandSweep:
    def test_last_key_varies_fastest(self) -> None:
        spec = {"a": [1, 2], "b": ["x", "y"]}
        assert expand_sweep(spec) == [
            {"a": 1, "b": "x"},
            {"a": 1, "b": "y"},
            {"a": 2, "b": "x"},
            {"a": 2, "b": "y"},
        ]

    def test_declaration_order_three_keys(self) -> None:
        spec = {"a": [1, 2], "b": [True], "c": [0.1, 0.2]}
        assert expand_sweep(spec) == [
            {"a": 1, "b": True, "c": 0.1},
            {"a": 1, "b": True, "c": 0.2},
            {"a": 2, "b": True, "c": 0.1},
            {"a": 2, "b": True, "c": 0.2},
        ]

    def test_single_key(self) -> None:
        assert expand_sweep({"a": [1, 2, 3]}) == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_size_is_the_cartesian_product(self) -> None:
        assert sweep_size({"a": [1, 2], "b": [1, 2, 3], "c": [1]}) == 6


class TestValidateSweepSpec:
    def test_scalars_accepted(self) -> None:
        validate_sweep_spec({"a": ["x", 1, 0.5, True]})  # no raise

    def test_empty_spec_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one param"):
            validate_sweep_spec({})

    def test_empty_value_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="sweep param 'a' needs at least one value"):
            validate_sweep_spec({"a": []})

    def test_non_scalar_values_rejected(self) -> None:
        for bad in (None, [1], {"x": 1}):
            with pytest.raises(ValueError, match="must be scalars"):
                validate_sweep_spec({"a": [1, bad]})

    def test_cap_boundary(self) -> None:
        validate_sweep_spec({"a": list(range(10)), "b": list(range(5))})  # 50: allowed
        with pytest.raises(ValueError, match=f"the cap is {MAX_SWEEP_POINTS}"):
            validate_sweep_spec({"a": list(range(51))})
        with pytest.raises(ValueError, match="64 points"):
            validate_sweep_spec({"a": list(range(8)), "b": list(range(8))})


class TestSchema:
    def test_absent_by_default(self) -> None:
        assert BacktestIn(strategy="s", pair="BTC/EUR").sweep is None

    def test_payload_round_trip(self) -> None:
        body = BacktestIn(strategy="s", pair="BTC/EUR", sweep={"a": [1, 2], "b": ["x", True, 0.5]})
        assert BacktestIn.model_validate(body.model_dump(mode="json")) == body

    def test_invalid_specs_rejected(self) -> None:
        for spec in ({}, {"a": []}, {"a": list(range(51))}, {"a": [None]}, {"a": [[1]]}):
            with pytest.raises(ValidationError):
                BacktestIn(strategy="s", pair="BTC/EUR", sweep=spec)

    def test_not_combinable_with_stability_windows(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined"):
            BacktestIn(strategy="s", pair="BTC/EUR", sweep={"a": [1]}, stability_windows=2)


class TestValidateSweepKeys:
    def test_valid_keys_pass(self) -> None:
        strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
        validate_sweep_keys(strategy, {}, {"adx_period": [7, 14], "adx_threshold": [20.0, 30.0]})

    def test_unknown_key_rejected_with_allowed_list(self) -> None:
        strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
        with pytest.raises(ValueError, match=r"invalid sweep.*Unknown params.*'nope'.*adx_period"):
            validate_sweep_keys(strategy, {}, {"nope": [1, 2]})

    def test_bad_first_value_rejected(self) -> None:
        strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
        with pytest.raises(ValueError, match="invalid sweep"):
            validate_sweep_keys(strategy, {}, {"adx_period": [0, 14]})  # gt=0

    def test_base_params_validated_with_the_point(self) -> None:
        strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
        with pytest.raises(ValueError, match="Unknown params"):
            validate_sweep_keys(strategy, {"nope": 1}, {"adx_period": [7]})


class TestMarker:
    def test_marker(self) -> None:
        assert sweep_marker("g1", {"a": 1}) == {"group": "g1", "point": {"a": 1}}


class TestRunSweep:
    async def test_points_run_in_grid_order_with_merged_params_and_markers(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[BacktestRequest] = []

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            seen.append(request)
            return RunId(f"run-{len(seen)}"), None, {"sharpe": 1.0}

        monkeypatch.setattr(sweep_mod, "run_backtest", fake_run)
        spec = {"adx_threshold": [20.0, 30.0], "entry_z_score": [1.0, 2.0]}
        first_run_id, result = await run_sweep(_request(), sessionmaker, group="g1", spec=spec)

        points = expand_sweep(spec)
        assert [r.params for r in seen] == [{"adx_period": 7, **point} for point in points]
        assert [r.sweep for r in seen] == [{"group": "g1", "point": point} for point in points]
        assert first_run_id == "run-1"
        assert result == {
            "sweep": [
                {"params": point, "run_id": f"run-{i + 1}", "metrics": {"sharpe": 1.0}}
                for i, point in enumerate(points)
            ]
        }

    async def test_swept_value_overrides_the_base_param(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[BacktestRequest] = []

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            seen.append(request)
            return RunId("run-1"), None, {}

        monkeypatch.setattr(sweep_mod, "run_backtest", fake_run)
        await run_sweep(_request(), sessionmaker, group="g1", spec={"adx_period": [14, 21]})
        assert [r.params for r in seen] == [{"adx_period": 14}, {"adx_period": 21}]

    async def test_point_failure_degrades_to_error_entry(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("No kraken candles for BTC/EUR 1h in range")
            return RunId(f"run-{calls}"), None, {"sharpe": 1.0}

        monkeypatch.setattr(sweep_mod, "run_backtest", fake_run)
        first_run_id, result = await run_sweep(
            _request(), sessionmaker, group="g1", spec={"adx_period": [7, 14, 21]}
        )

        assert calls == 3  # one failure does not stop the other points
        assert first_run_id == "run-1"
        ok, bad, ok2 = result["sweep"]
        assert ok["run_id"] == "run-1" and "error" not in ok
        assert bad["params"] == {"adx_period": 14}
        assert bad["error"].startswith("ValueError: No kraken candles")
        assert "run_id" not in bad and "metrics" not in bad
        assert ok2["run_id"] == "run-3"

    async def test_first_run_id_is_the_first_successful_point(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("boom")
            return RunId("run-2"), None, {}

        monkeypatch.setattr(sweep_mod, "run_backtest", fake_run)
        first_run_id, result = await run_sweep(
            _request(), sessionmaker, group="g1", spec={"adx_period": [7, 14]}
        )
        assert first_run_id == "run-2"
        assert "error" in result["sweep"][0]
        assert result["sweep"][1]["run_id"] == "run-2"

    async def test_all_points_failing_returns_no_run_id(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_run(request: BacktestRequest, sm: Any) -> Any:
            raise ValueError("No kraken candles for BTC/EUR 1h in range")

        monkeypatch.setattr(sweep_mod, "run_backtest", fake_run)
        first_run_id, result = await run_sweep(
            _request(), sessionmaker, group="g1", spec={"adx_period": [7, 14]}
        )
        assert first_run_id is None
        assert [set(entry) for entry in result["sweep"]] == [{"params", "error"}] * 2


class TestWorkerSweep:
    async def _run_worker(
        self, sessionmaker: async_sessionmaker[AsyncSession], body: BacktestIn
    ) -> BacktestJobRow:
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
            return row

    async def test_job_completes_with_sweep_aggregation(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        aggregation = {
            "sweep": [
                {"params": {"adx_period": 7}, "run_id": "run-p0", "metrics": {"sharpe": 1.0}},
                {"params": {"adx_period": 14}, "run_id": "run-p1", "metrics": {"sharpe": 0.5}},
            ]
        }

        async def fake_sweep(
            request: BacktestRequest, sm: Any, *, group: str, spec: dict[str, list[Any]]
        ) -> Any:
            seen["request"] = request
            seen["group"] = group
            seen["spec"] = spec
            return "run-p0", aggregation

        monkeypatch.setattr(worker_mod, "run_sweep", fake_sweep)

        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            start=BASE,
            end=BASE + timedelta(hours=48),
            sweep={"adx_period": [7, 14]},
        )
        row = await self._run_worker(sessionmaker, body)

        # the base request carries no marker; run_sweep marks each point
        assert seen["request"].sweep is None
        assert seen["request"].stability is None
        assert seen["group"] == row.id
        assert seen["spec"] == {"adx_period": [7, 14]}
        assert row.status == STATUS_COMPLETED
        assert row.run_id == "run-p0"  # the first point's run
        assert row.result == aggregation

    async def test_all_points_failed_still_completes(
        self, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aggregation = {
            "sweep": [
                {"params": {"adx_period": 7}, "error": "ValueError: no candles"},
                {"params": {"adx_period": 14}, "error": "ValueError: no candles"},
            ]
        }

        async def fake_sweep(
            request: BacktestRequest, sm: Any, *, group: str, spec: dict[str, list[Any]]
        ) -> Any:
            return None, aggregation

        monkeypatch.setattr(worker_mod, "run_sweep", fake_sweep)

        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            start=BASE,
            end=BASE + timedelta(hours=48),
            sweep={"adx_period": [7, 14]},
        )
        row = await self._run_worker(sessionmaker, body)

        assert row.status == STATUS_COMPLETED
        assert row.run_id is None
        assert row.result == aggregation


class TestGetBacktestSweepShape:
    async def _seed_job(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        run_id: str | None,
        result: dict[str, Any] | None,
    ) -> str:
        now = utc_now()
        async with sm_scope(sessionmaker) as session:
            if run_id is not None:
                session.add(
                    RunRow(
                        id=run_id,
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
                    run_id=run_id,
                    result=result,
                )
            )
        return "job-1"

    async def test_completed_with_sweep(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        aggregation = {
            "sweep": [
                {"params": {"adx_period": 7}, "run_id": "run-1", "metrics": {"sharpe": 1.0}},
                {"params": {"adx_period": 14}, "error": "ValueError: boom"},
            ]
        }
        job_id = await self._seed_job(sessionmaker, "run-1", aggregation)
        async with sm_scope(sessionmaker) as session:
            body = await get_backtest(None, session, job_id)
        assert body["status"] == "completed"
        assert body["run"]["id"] == "run-1"  # the first point's run
        assert body["sweep"] == aggregation["sweep"]
        assert body["stability"] is None

    async def test_completed_sweep_all_points_failed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        aggregation = {"sweep": [{"params": {"adx_period": 7}, "error": "ValueError: boom"}]}
        job_id = await self._seed_job(sessionmaker, None, aggregation)
        async with sm_scope(sessionmaker) as session:
            body = await get_backtest(None, session, job_id)
        assert body["status"] == "completed"  # not "run row missing"
        assert body["run"] is None
        assert body["sweep"] == aggregation["sweep"]

    async def test_completed_without_sweep_or_stability(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job_id = await self._seed_job(sessionmaker, "run-1", None)
        async with sm_scope(sessionmaker) as session:
            body = await get_backtest(None, session, job_id)
        assert body["status"] == "completed"
        assert body["sweep"] is None
        assert body["stability"] is None


class TestPlanValidation:
    def test_build_request_rejects_unknown_sweep_key(self) -> None:
        strategies = lint_and_load_strategies(EXAMPLES_DIR)
        body = BacktestIn(strategy="regime-switch", pair="BTC/EUR", sweep={"nope": [1, 2]})
        with pytest.raises(ValueError, match="invalid sweep"):
            build_backtest_request(body, strategies)

    def test_build_request_accepts_a_valid_sweep(self) -> None:
        strategies = lint_and_load_strategies(EXAMPLES_DIR)
        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            params={"adx_period": 7},
            sweep={"adx_threshold": [20.0, 30.0]},
        )
        request = build_backtest_request(body, strategies)
        assert request.params == {"adx_period": 7}  # the grid applies per point, not here
