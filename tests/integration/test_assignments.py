"""Run assignments: repository, /api/v1/assignments routes, settings facade, supervisor."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import get_settings
from kaupo.core import supervisor as sup
from kaupo.core.runner import DbControlProbe
from kaupo.data import assignments as assignments_repo
from kaupo.db.models import EventRow, RunRow
from kaupo.db.session import dispose_engine, get_sessionmaker, sm_scope
from kaupo.domain import new_id, utc_now
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    # auth disabled for these tests (restore afterwards)
    saved = {k: os.environ.get(k) for k in ("KAUPO_ADMIN_TOKEN", "KAUPO_READONLY_TOKEN")}
    os.environ.pop("KAUPO_ADMIN_TOKEN", None)
    os.environ.pop("KAUPO_READONLY_TOKEN", None)
    get_settings.cache_clear()
    await dispose_engine()

    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _add_run(
    session: AsyncSession,
    run_id: str,
    mode: str,
    status: str,
    pair: str = "BTC/EUR",
    strategy_id: str = "regime-switch",
) -> None:
    session.add(
        RunRow(
            id=run_id,
            mode=mode,
            strategy_id=strategy_id,
            strategy_version="v",
            started_at=utc_now(),
            status=status,
            config={"pair": pair},
        )
    )


async def _run_status(run_id: str) -> str | None:
    async with sm_scope(get_sessionmaker()) as s:
        row = await s.get(RunRow, run_id)
        return row.status if row is not None else None


async def _wait_for(cond: Callable[[], object], seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        result = cond()
        if await result if asyncio.iscoroutine(result) else result:
            return
        assert loop.time() < deadline, "timed out waiting for condition"
        await asyncio.sleep(0.02)


# --- repository -----------------------------------------------------------


async def test_create_and_get(session: AsyncSession) -> None:
    created = await assignments_repo.create_assignment(
        session,
        id="a1",
        strategy_id="regime-switch",
        pair="BTC/EUR",
        timeframe="1h",
        params={"adx_threshold": 30},
        starting_cash=5000.0,
    )
    await session.commit()
    assert created.mode == "shadow"  # default
    assert created.enabled is True

    row = await assignments_repo.get_assignment(session, "a1")
    assert row is not None
    assert row.strategy_id == "regime-switch"
    assert row.params == {"adx_threshold": 30}
    assert row.starting_cash == 5000.0
    assert row.created_at == row.updated_at

    assert await assignments_repo.get_assignment(session, "missing") is None


async def test_list_and_enabled_only(session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await assignments_repo.create_assignment(
        session, id="a2", strategy_id="sma-cross", pair="SOL/EUR", timeframe="4h", enabled=False
    )
    await session.commit()

    assert [a.id for a in await assignments_repo.list_assignments(session)] == ["a1", "a2"]
    enabled = await assignments_repo.list_assignments(session, enabled_only=True)
    assert [a.id for a in enabled] == ["a1"]


async def test_update_bumps_updated_at(session: AsyncSession) -> None:
    created = await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    updated = await assignments_repo.update_assignment(session, "a1", timeframe="4h", enabled=False)
    await session.commit()
    assert updated is not None
    assert updated.timeframe == "4h"
    assert updated.enabled is False
    assert updated.pair == "BTC/EUR"  # untouched field survives
    assert updated.updated_at >= created.updated_at

    assert await assignments_repo.update_assignment(session, "missing", timeframe="4h") is None


async def test_delete_disables(session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    deleted = await assignments_repo.delete_assignment(session, "a1")
    await session.commit()
    assert deleted is not None
    assert deleted.enabled is False
    # the row survives: it is a soft delete
    assert (await assignments_repo.get_assignment(session, "a1")) is not None
    assert await assignments_repo.delete_assignment(session, "missing") is None


# --- API ------------------------------------------------------------------


async def test_api_list_empty(client: AsyncClient) -> None:
    r = await client.get("/api/v1/assignments")
    assert r.status_code == 200
    assert r.json() == []


async def test_api_create_normalizes_and_generates_id(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/assignments",
        json={"strategy_id": "regime-switch", "pair": "btc/eur", "timeframe": "1h"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["pair"] == "BTC/EUR"  # normalized
    assert body["mode"] == "shadow"
    assert body["enabled"] is True
    assert body["run_id"] is None
    assert body["id"]  # generated


async def test_api_list_shows_the_matching_live_run(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.post(
        "/api/v1/assignments",
        json={"id": "a1", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"},
    )
    assert r.status_code == 201
    _add_run(session, "run-1", "shadow", "running", pair="BTC/EUR")
    _add_run(session, "run-2", "shadow", "running", pair="SOL/EUR")  # other pair
    _add_run(session, "run-3", "shadow", "halted", pair="BTC/EUR")  # not running
    _add_run(session, "run-4", "backtest", "running", pair="BTC/EUR")  # other mode
    await session.commit()

    r = await client.get("/api/v1/assignments")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "a1"
    assert rows[0]["run_id"] == "run-1"


async def test_api_create_conflict(client: AsyncClient) -> None:
    payload = {"id": "a1", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"}
    assert (await client.post("/api/v1/assignments", json=payload)).status_code == 201
    r = await client.post("/api/v1/assignments", json=payload)
    assert r.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy_id": "does-not-exist", "pair": "BTC/EUR", "timeframe": "1h"},
        {"strategy_id": "regime-switch", "pair": "BTCEUR", "timeframe": "1h"},
        {"strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "2h"},
        {"strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h", "mode": "moon"},
        {"strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h", "params": {"bogus": 1}},
        {"strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h", "starting_cash": -5},
    ],
)
async def test_api_create_validation(client: AsyncClient, payload: dict) -> None:
    r = await client.post("/api/v1/assignments", json=payload)
    assert r.status_code == 422
    rows = (await client.get("/api/v1/assignments")).json()
    assert rows == []


async def test_api_update(client: AsyncClient, session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    r = await client.put(
        "/api/v1/assignments/a1",
        json={"timeframe": "4h", "params": {"adx_threshold": 30}, "starting_cash": 5000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["timeframe"] == "4h"
    assert body["params"] == {"adx_threshold": 30}
    assert body["starting_cash"] == 5000.0
    assert body["pair"] == "BTC/EUR"  # untouched

    session.expire_all()  # the API wrote from another session
    row = await assignments_repo.get_assignment(session, "a1")
    assert row is not None and row.timeframe == "4h"


async def test_api_update_not_found_and_validation(client: AsyncClient, session: AsyncSession) -> None:
    assert (await client.put("/api/v1/assignments/missing", json={"enabled": False})).status_code == 404

    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()
    assert (await client.put("/api/v1/assignments/a1", json={})).status_code == 422
    assert (await client.put("/api/v1/assignments/a1", json={"strategy_id": "nope"})).status_code == 422
    # params validated against the (merged) strategy
    assert (await client.put("/api/v1/assignments/a1", json={"params": {"bogus": 1}})).status_code == 422
    session.expire_all()  # the API wrote from another session
    row = await assignments_repo.get_assignment(session, "a1")
    assert row is not None and row.timeframe == "1h"  # nothing stored


async def test_api_delete_disables(client: AsyncClient, session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    r = await client.delete("/api/v1/assignments/a1")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    rows = (await client.get("/api/v1/assignments")).json()
    assert len(rows) == 1 and rows[0]["enabled"] is False  # soft delete

    assert (await client.delete("/api/v1/assignments/missing")).status_code == 404


# --- settings facade --------------------------------------------------------


async def test_put_settings_creates_then_updates_primary_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    r = await client.put(
        "/api/v1/settings", json={"shadow_strategy": "regime-switch", "shadow_pair": "eth/eur"}
    )
    assert r.status_code == 200

    row = await assignments_repo.get_assignment(session, "primary")
    assert row is not None
    assert row.strategy_id == "regime-switch"
    assert row.pair == "ETH/EUR"
    assert row.timeframe == "1h"  # built-in default filled in
    assert row.mode == "shadow"
    assert row.enabled is True

    r = await client.put("/api/v1/settings", json={"shadow_timeframe": "4h"})
    assert r.status_code == 200
    session.expire_all()
    row = await assignments_repo.get_assignment(session, "primary")
    assert row is not None
    assert row.timeframe == "4h"
    assert row.pair == "ETH/EUR"  # earlier value kept


async def test_put_settings_without_changes_leaves_primary_row_untouched(
    client: AsyncClient, session: AsyncSession
) -> None:
    from kaupo.data import settings as settings_repo

    await settings_repo.upsert_settings(session, {"shadow_timeframe": "4h"})
    await assignments_repo.create_assignment(
        session, id="primary", strategy_id="sma-cross", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    # no effective change -> no sync (an update would clobber the manual row)
    r = await client.put("/api/v1/settings", json={"shadow_timeframe": "4h"})
    assert r.status_code == 200
    row = await assignments_repo.get_assignment(session, "primary")
    assert row is not None and row.strategy_id == "sma-cross"


# --- orphan cleanup ---------------------------------------------------------


async def test_halt_orphan_runs(session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await assignments_repo.create_assignment(
        session, id="a2", strategy_id="regime-switch", pair="ETH/EUR", timeframe="1h", enabled=False
    )
    _add_run(session, "keep", "shadow", "running", pair="BTC/EUR")
    _add_run(session, "orphan", "shadow", "running", pair="SOL/EUR", strategy_id="sma-cross")
    # a disabled assignment does not protect its row
    _add_run(session, "orphan-disabled", "shadow", "running", pair="ETH/EUR")
    _add_run(session, "bt", "backtest", "running", pair="SOL/EUR", strategy_id="sma-cross")
    _add_run(session, "old", "shadow", "halted", pair="SOL/EUR", strategy_id="sma-cross")
    await session.commit()

    halted = await sup.halt_orphan_runs(session)
    await session.commit()
    assert halted == 2

    rows = {r.id: r for r in (await session.execute(select(RunRow))).scalars().all()}
    assert rows["keep"].status == "running"
    assert rows["orphan"].status == "halted"
    assert rows["orphan"].metrics == {"halt_reason": "no matching assignment"}
    assert rows["orphan"].ended_at is not None
    assert rows["orphan-disabled"].status == "halted"
    assert rows["bt"].status == "running"  # other modes untouched
    assert rows["old"].status == "halted"


# --- supervisor loop (fake exchange and engine) ----------------------------


class _FakeKrakenClient:
    async def __aenter__(self) -> "_FakeKrakenClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def _patch_supervisor(monkeypatch: pytest.MonkeyPatch, started: list[tuple[str, str]]) -> None:
    """Fake run_shadow: records a run row, then runs until stopped or killed."""

    async def fake_run_shadow(request, sm, client, stop):
        run_id = new_id()
        started.append((request.assignment_id, run_id))
        async with sm_scope(sm) as s:
            s.add(
                RunRow(
                    id=run_id,
                    mode="shadow",
                    strategy_id=request.strategy.id,
                    strategy_version="v",
                    started_at=utc_now(),
                    status="running",
                    config={"pair": str(request.pair), "assignment_id": request.assignment_id},
                )
            )
        probe = DbControlProbe(sm, run_id)
        while not stop.is_set():
            if await probe() in ("kill", "switch"):
                break
            await asyncio.sleep(0.01)
        async with sm_scope(sm) as s:
            row = await s.get(RunRow, run_id)
            if row is not None:
                row.status = "halted"
                row.ended_at = utc_now()

    monkeypatch.setattr(sup, "run_shadow", fake_run_shadow)
    monkeypatch.setattr(sup, "KrakenClient", _FakeKrakenClient)


async def _control_event(command: str, run_id: str | None) -> None:
    async with sm_scope(get_sessionmaker()) as s:
        s.add(
            EventRow(
                id=new_id(),
                ts=utc_now(),
                level="info",
                source="control",
                message=f"control command {command!r} issued for run {run_id or 'ALL'}",
                data={"command": command, "run_id": run_id},
            )
        )


async def test_supervisor_starts_restarts_on_change_and_stops_on_disable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    stop = asyncio.Event()
    task = asyncio.create_task(
        sup.run_supervisor(
            get_sessionmaker(),
            load_strategies(EXAMPLES_DIR),
            stop,
            reconcile_interval_seconds=0.05,
            restart_backoff=timedelta(seconds=0.2),
        )
    )
    try:
        await _wait_for(lambda: len(started) == 1)
        assert started[0][0] == "a1"

        # a config change stops the old run and starts a new one
        async with sm_scope(get_sessionmaker()) as s:
            await assignments_repo.update_assignment(s, "a1", params={"adx_threshold": 30})
        await _wait_for(lambda: len(started) == 2)
        assert await _run_status(started[0][1]) == "halted"

        # disabling stops the run; nothing new starts
        async with sm_scope(get_sessionmaker()) as s:
            await assignments_repo.delete_assignment(s, "a1")

        async def second_halted() -> bool:
            return (await _run_status(started[1][1])) == "halted"

        await _wait_for(second_halted)
        await asyncio.sleep(0.3)  # several reconcile passes
        assert len(started) == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_supervisor_kill_stays_down_until_resume(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="regime-switch", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    stop = asyncio.Event()
    task = asyncio.create_task(
        sup.run_supervisor(
            get_sessionmaker(),
            load_strategies(EXAMPLES_DIR),
            stop,
            reconcile_interval_seconds=0.05,
            restart_backoff=timedelta(seconds=0.2),
        )
    )
    try:
        await _wait_for(lambda: len(started) == 1)
        run_id = started[0][1]

        # a kill through the control channel ends the run; it stays down
        await _control_event("kill", run_id)

        async def first_halted() -> bool:
            return (await _run_status(run_id)) == "halted"

        await _wait_for(first_halted)
        await asyncio.sleep(0.5)  # many reconcile passes: no restart
        assert len(started) == 1

        # a resume command targeting the run brings it back
        await _control_event("resume", run_id)
        await _wait_for(lambda: len(started) == 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_supervisor_does_not_start_unknown_strategy(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    await assignments_repo.create_assignment(
        session, id="a1", strategy_id="not-on-disk", pair="BTC/EUR", timeframe="1h"
    )
    await session.commit()

    stop = asyncio.Event()
    task = asyncio.create_task(
        sup.run_supervisor(
            get_sessionmaker(),
            load_strategies(EXAMPLES_DIR),
            stop,
            reconcile_interval_seconds=0.05,
            restart_backoff=timedelta(seconds=0.2),
        )
    )
    try:
        await asyncio.sleep(0.4)  # several reconcile passes, no hot loop
        assert started == []
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
