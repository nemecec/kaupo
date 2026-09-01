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
from kaupo.core.recorder import WATCHDOG_HALT_REASON
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
    timeframe: str = "1h",
) -> None:
    session.add(
        RunRow(
            id=run_id,
            mode=mode,
            strategy_id=strategy_id,
            strategy_version="v",
            started_at=utc_now(),
            status=status,
            config={"pair": pair, "timeframe": timeframe},
        )
    )


async def _run_status(run_id: str) -> str | None:
    async with sm_scope(get_sessionmaker()) as s:
        row = await s.get(RunRow, run_id)
        return row.status if row is not None else None


async def _wait_for(cond: Callable[[], object], seconds: float = 30.0) -> None:
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


async def test_create_portfolio_assignment_derives_the_joined_pair(session: AsyncSession) -> None:
    created = await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",  # derived from the universe
        timeframe="1h",
        pairs=["sol/eur", "BTC/EUR"],  # unsorted, unnormalized on purpose
    )
    await session.commit()
    assert created.pairs == ["BTC/EUR", "SOL/EUR"]  # canonical sorted order
    assert created.pair == "BTC/EUR,SOL/EUR"  # joined universe, same as the run config

    row = await assignments_repo.get_assignment(session, "pf")
    assert row is not None and row.pairs == ["BTC/EUR", "SOL/EUR"]


async def test_create_portfolio_assignment_validates_the_universe(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="one quote currency"):
        await assignments_repo.create_assignment(
            session,
            id="bad",
            strategy_id="momentum-rotation",
            pair="",
            timeframe="1h",
            pairs=["BTC/EUR", "SOL/USD"],
        )


async def test_update_pairs_rewrites_the_joined_pair(session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",
        timeframe="1h",
        pairs=["BTC/EUR", "SOL/EUR"],
    )
    await session.commit()

    updated = await assignments_repo.update_assignment(session, "pf", pairs=["ada/eur", "BTC/EUR"])
    await session.commit()
    assert updated is not None
    assert updated.pairs == ["ADA/EUR", "BTC/EUR"]
    assert updated.pair == "ADA/EUR,BTC/EUR"

    # a bare pair update switches the row back to single-pair (pair and pairs never diverge)
    updated = await assignments_repo.update_assignment(session, "pf", pair="ETH/EUR")
    await session.commit()
    assert updated is not None
    assert updated.pairs is None
    assert updated.pair == "ETH/EUR"


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


async def test_api_list_links_same_pair_runs_by_timeframe(client: AsyncClient, session: AsyncSession) -> None:
    """Same strategy and pair on two timeframes: each assignment gets its own run."""
    await client.post(
        "/api/v1/assignments",
        json={"id": "a-1h", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"},
    )
    await client.post(
        "/api/v1/assignments",
        json={"id": "a-4h", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "4h"},
    )
    _add_run(session, "run-1h", "shadow", "running", timeframe="1h")
    _add_run(session, "run-4h", "shadow", "running", timeframe="4h")
    await session.commit()

    r = await client.get("/api/v1/assignments")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert rows["a-1h"]["run_id"] == "run-1h"
    assert rows["a-4h"]["run_id"] == "run-4h"


async def test_api_create_conflict(client: AsyncClient) -> None:
    payload = {"id": "a1", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"}
    assert (await client.post("/api/v1/assignments", json=payload)).status_code == 201
    r = await client.post("/api/v1/assignments", json=payload)
    assert r.status_code == 409


async def test_api_create_with_pairs(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/assignments",
        json={
            "id": "pf",
            "strategy_id": "momentum-rotation",
            "pairs": ["SOL/EUR", "btc/eur"],  # unsorted, unnormalized on purpose
            "timeframe": "1h",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["pairs"] == ["BTC/EUR", "SOL/EUR"]
    assert body["pair"] == "BTC/EUR,SOL/EUR"  # joined universe stored in pair
    assert body["mode"] == "shadow"

    rows = (await client.get("/api/v1/assignments")).json()
    assert len(rows) == 1
    assert rows[0]["pairs"] == ["BTC/EUR", "SOL/EUR"]


@pytest.mark.parametrize(
    "payload",
    [
        # both pair and pairs
        {
            "strategy_id": "momentum-rotation",
            "pair": "BTC/EUR",
            "pairs": ["BTC/EUR", "SOL/EUR"],
            "timeframe": "1h",
        },
        # neither pair nor pairs
        {"strategy_id": "momentum-rotation", "timeframe": "1h"},
        # a single pair is not a universe
        {"strategy_id": "momentum-rotation", "pairs": ["BTC/EUR"], "timeframe": "1h"},
        # one shared quote required
        {"strategy_id": "momentum-rotation", "pairs": ["BTC/EUR", "SOL/USD"], "timeframe": "1h"},
        # no duplicates
        {"strategy_id": "momentum-rotation", "pairs": ["BTC/EUR", "btc/eur"], "timeframe": "1h"},
        # a portfolio universe needs a portfolio strategy
        {"strategy_id": "regime-switch", "pairs": ["BTC/EUR", "SOL/EUR"], "timeframe": "1h"},
        # a portfolio strategy needs a universe
        {"strategy_id": "momentum-rotation", "pair": "BTC/EUR", "timeframe": "1h"},
    ],
)
async def test_api_create_pairs_validation(client: AsyncClient, payload: dict) -> None:
    r = await client.post("/api/v1/assignments", json=payload)
    assert r.status_code == 422
    rows = (await client.get("/api/v1/assignments")).json()
    assert rows == []


async def test_api_update_pairs(client: AsyncClient, session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",
        timeframe="1h",
        pairs=["BTC/EUR", "SOL/EUR"],
    )
    await session.commit()

    r = await client.put("/api/v1/assignments/pf", json={"pairs": ["ada/eur", "BTC/EUR"]})
    assert r.status_code == 200
    body = r.json()
    assert body["pairs"] == ["ADA/EUR", "BTC/EUR"]
    assert body["pair"] == "ADA/EUR,BTC/EUR"  # joined universe rewritten

    # switching strategy and pair together turns it back into a single-pair row
    r = await client.put("/api/v1/assignments/pf", json={"strategy_id": "regime-switch", "pair": "btc/eur"})
    assert r.status_code == 200
    body = r.json()
    assert body["pair"] == "BTC/EUR"
    assert body["pairs"] is None

    session.expire_all()  # the API wrote from another session
    row = await assignments_repo.get_assignment(session, "pf")
    assert row is not None and row.pairs is None and row.pair == "BTC/EUR"


async def test_api_update_pairs_validation(client: AsyncClient, session: AsyncSession) -> None:
    await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",
        timeframe="1h",
        pairs=["BTC/EUR", "SOL/EUR"],
    )
    await session.commit()

    # mixed quotes
    r = await client.put("/api/v1/assignments/pf", json={"pairs": ["BTC/EUR", "SOL/USD"]})
    assert r.status_code == 422
    # pair and pairs together
    r = await client.put("/api/v1/assignments/pf", json={"pair": "BTC/EUR", "pairs": ["BTC/EUR", "SOL/EUR"]})
    assert r.status_code == 422
    # a universe update keeps the portfolio-strategy requirement
    r = await client.put("/api/v1/assignments/pf", json={"strategy_id": "regime-switch"})
    assert r.status_code == 422

    session.expire_all()
    row = await assignments_repo.get_assignment(session, "pf")
    assert row is not None and row.pairs == ["BTC/EUR", "SOL/EUR"]  # nothing stored


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


async def test_halt_orphan_runs_distinguishes_timeframes(session: AsyncSession) -> None:
    """Same strategy and pair on different timeframes are different slots."""
    await assignments_repo.create_assignment(
        session, id="a-1h", strategy_id="sma-cross", pair="BTC/EUR", timeframe="1h"
    )
    await assignments_repo.create_assignment(
        session, id="a-4h", strategy_id="sma-cross", pair="BTC/EUR", timeframe="4h"
    )
    _add_run(session, "run-1h", "shadow", "running", strategy_id="sma-cross", timeframe="1h")
    _add_run(session, "run-4h", "shadow", "running", strategy_id="sma-cross", timeframe="4h")
    # no 1d assignment protects this row
    _add_run(session, "run-1d", "shadow", "running", strategy_id="sma-cross", timeframe="1d")
    await session.commit()

    halted = await sup.halt_orphan_runs(session)
    await session.commit()
    assert halted == 1

    rows = {r.id: r for r in (await session.execute(select(RunRow))).scalars().all()}
    assert rows["run-1h"].status == "running"
    assert rows["run-4h"].status == "running"
    assert rows["run-1d"].status == "halted"
    assert rows["run-1d"].metrics == {"halt_reason": "no matching assignment"}


# --- supervisor loop (fake exchange and engine) ----------------------------


class _FakeKrakenClient:
    async def __aenter__(self) -> "_FakeKrakenClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def _patch_supervisor(monkeypatch: pytest.MonkeyPatch, started: list[tuple[str, str]]) -> None:
    """Fake run_shadow/run_portfolio_shadow: record a run row, then run until stopped or killed."""

    async def fake_run(request, sm, client, stop, funding_client=None):
        run_id = new_id()
        if hasattr(request, "pairs"):
            config: dict = {
                "pair": ",".join(str(p) for p in request.pairs),
                "pairs": [str(p) for p in request.pairs],
            }
        else:
            config = {"pair": str(request.pair)}
        config["assignment_id"] = request.assignment_id
        async with sm_scope(sm) as s:
            s.add(
                RunRow(
                    id=run_id,
                    mode="shadow",
                    strategy_id=request.strategy.id,
                    strategy_version="v",
                    started_at=utc_now(),
                    status="running",
                    config=config,
                )
            )
        probe = DbControlProbe(sm, run_id)
        # advertise the run only after the probe exists: the probe ignores
        # commands older than its creation time, so a control event that
        # lands earlier is lost forever (CI flake under slow DB inserts)
        started.append((request.assignment_id, run_id))
        while not stop.is_set():
            if await probe() in ("kill", "switch"):
                break
            await asyncio.sleep(0.01)
        async with sm_scope(sm) as s:
            row = await s.get(RunRow, run_id)
            if row is not None:
                row.status = "halted"
                row.ended_at = utc_now()

    monkeypatch.setattr(sup, "run_shadow", fake_run)
    monkeypatch.setattr(sup, "run_portfolio_shadow", fake_run)
    monkeypatch.setattr(sup, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(sup, "BinanceClient", _FakeKrakenClient)


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


async def test_supervisor_watchdog_restarts_a_stalled_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that stops writing equity snapshots is cancelled and restarted (kaupo#31)."""
    started: list[tuple[str, str]] = []

    async def fake_run(request, sm, client, stop, funding_client=None):
        run_id = new_id()
        # the first run looks three hours old with zero snapshots (stalled);
        # the restart is fresh and healthy
        started_at = utc_now() - timedelta(hours=3) if not started else utc_now()
        config = {"pair": str(request.pair), "timeframe": request.timeframe.value}
        config["assignment_id"] = request.assignment_id
        async with sm_scope(sm) as s:
            s.add(
                RunRow(
                    id=run_id,
                    mode="shadow",
                    strategy_id=request.strategy.id,
                    strategy_version="v",
                    started_at=started_at,
                    status="running",
                    config=config,
                )
            )
        started.append((request.assignment_id, run_id))
        try:
            await stop.wait()  # wedged on progress: no candles, no snapshots
        finally:
            async with sm_scope(sm) as s:
                row = await s.get(RunRow, run_id)
                if row is not None:
                    row.status = "halted"
                    row.ended_at = utc_now()

    monkeypatch.setattr(sup, "run_shadow", fake_run)
    monkeypatch.setattr(sup, "run_portfolio_shadow", fake_run)
    monkeypatch.setattr(sup, "KrakenClient", _FakeKrakenClient)
    monkeypatch.setattr(sup, "BinanceClient", _FakeKrakenClient)

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
        # watchdog cancels the stalled run; the backoff path restarts it
        await _wait_for(lambda: len(started) == 2)
        assert started[0][0] == "a1"
        assert started[1][0] == "a1"
        assert await _run_status(started[0][1]) == "halted"

        # the cancelled row carries the watchdog reason, so the successor
        # resumes the ledger chain instead of starting flat (kaupo#33)
        async def marked() -> bool:
            async with sm_scope(get_sessionmaker()) as s:
                row = await s.get(RunRow, started[0][1])
                return row is not None and (row.metrics or {}).get("halt_reason") == WATCHDOG_HALT_REASON

        await _wait_for(marked)
        # the fresh run is not watchdog-ed: nothing further starts
        await asyncio.sleep(0.5)
        assert len(started) == 2
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


async def test_supervisor_runs_a_portfolio_assignment_and_restarts_on_universe_change(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",
        timeframe="1h",
        pairs=["SOL/EUR", "BTC/EUR"],
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
        assert started[0][0] == "pf"

        # the run row carries the joined universe, like a real portfolio shadow run
        async def run_config_pair() -> str | None:
            async with sm_scope(get_sessionmaker()) as s:
                row = await s.get(RunRow, started[0][1])
                return (row.config or {}).get("pair") if row is not None else None

        assert await run_config_pair() == "BTC/EUR,SOL/EUR"

        # a universe change stops the old run and starts a new one
        async with sm_scope(get_sessionmaker()) as s:
            await assignments_repo.update_assignment(s, "pf", pairs=["ADA/EUR", "BTC/EUR"])
        await _wait_for(lambda: len(started) == 2)
        assert await _run_status(started[0][1]) == "halted"

        # disabling stops the run; nothing new starts
        async with sm_scope(get_sessionmaker()) as s:
            await assignments_repo.delete_assignment(s, "pf")

        async def second_halted() -> bool:
            return (await _run_status(started[1][1])) == "halted"

        await _wait_for(second_halted)
        await asyncio.sleep(0.3)  # several reconcile passes
        assert len(started) == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_supervisor_kill_and_resume_work_for_a_portfolio_assignment(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    await assignments_repo.create_assignment(
        session,
        id="pf",
        strategy_id="momentum-rotation",
        pair="",
        timeframe="1h",
        pairs=["BTC/EUR", "SOL/EUR"],
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

        await _control_event("kill", run_id)

        async def first_halted() -> bool:
            return (await _run_status(run_id)) == "halted"

        await _wait_for(first_halted)
        await asyncio.sleep(0.5)  # many reconcile passes: no restart
        assert len(started) == 1

        await _control_event("resume", run_id)
        await _wait_for(lambda: len(started) == 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_supervisor_does_not_start_a_pairs_assignment_with_a_single_pair_strategy(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []
    _patch_supervisor(monkeypatch, started)
    # the API rejects this combination; a hand-written row must not hot-loop either
    await assignments_repo.create_assignment(
        session,
        id="bad",
        strategy_id="regime-switch",
        pair="",
        timeframe="1h",
        pairs=["BTC/EUR", "SOL/EUR"],
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
