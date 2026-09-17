"""Forward registration, immutable contracts, and operator cost accounting."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from kaupo.api.routes import research
from kaupo.config import get_settings
from kaupo.core.provenance import engine_version
from kaupo.data.assignments import create_assignment
from kaupo.db.models import EventRow, OrderRow, RunRow

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
RESEARCH = {"Authorization": "Bearer research-test"}
ADMIN = {"Authorization": "Bearer admin-test"}
READONLY = {"Authorization": "Bearer readonly-test"}


@pytest.fixture
async def client(session, monkeypatch):
    monkeypatch.setenv("KAUPO_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("KAUPO_READONLY_TOKEN", "readonly-test")
    monkeypatch.setenv("KAUPO_RESEARCH_TOKEN", "research-test")
    monkeypatch.setattr(research, "utc_now", lambda: NOW)
    get_settings.cache_clear()
    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


async def seed(session, mode="shadow", **overrides):
    await create_assignment(
        session,
        id="slot",
        strategy_id="trend",
        pair="BTC/EUR",
        timeframe="1d",
        mode=mode,
        params={},
        enabled=True,
        starting_cash=10000,
    )
    row = RunRow(
        id="root",
        mode=mode,
        strategy_id="trend",
        strategy_version="v1",
        status="running",
        started_at=NOW - timedelta(minutes=1),
        config={
            "assignment_id": "slot",
            "pair": "BTC/EUR",
            "timeframe": "1d",
            "params": {},
            "starting_cash": 10000,
            "behaviour_hash": "behaviour",
            "engine_version": engine_version(),
            "fees": {"maker_bps": 40, "taker_bps": 80, "marketable_limit": "skip"},
            "risk": {"max_position_quote": 1000},
            **overrides,
        },
    )
    session.add(row)
    await session.commit()
    return row


def registration(**extra):
    return {
        "assignment_id": "slot",
        "hypothesis": "Slow momentum exceeds fees on unseen observations",
        **extra,
    }


async def test_trial_is_prospective_immutable_and_research_readable(client, session):
    await seed(session)
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 201, response.text
    trial = response.json()
    assert trial["registered_at"] == NOW.isoformat()
    assert trial["ends_at"] == (NOW + timedelta(days=90)).isoformat()
    assert trial["status"] == "collecting"
    assert trial["registered_trials_total"] == 1
    path = f"/api/v1/research/trials/{trial['id']}"
    assert (await client.get(path, headers=READONLY)).status_code == 200
    assert (await client.put(path, headers=ADMIN, json={})).status_code == 405
    assert (await client.delete(path, headers=ADMIN)).status_code == 405
    duplicate = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert duplicate.status_code == 409
    listing = await client.get("/api/v1/research/trials", headers=RESEARCH)
    assert len(listing.json()) == 1


async def test_cannot_backdate_or_lower_policy(client, session):
    await seed(session)
    for extra in ({"registered_at": "2020-01-01"}, {"policy": {"min_sharpe": -5}}):
        response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration(**extra))
        assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine_version": "old"},
        {"resumed_from": "older"},
        {"starting_cash": 100},
        {"fees": {"marketable_limit": "maker"}},
        {"timeframe": "4h"},
        {"risk": {}},
    ],
)
async def test_unproven_or_changed_run_cannot_register(client, session, overrides):
    await seed(session, **overrides)
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code in (409, 422), response.text


async def test_live_run_and_readonly_registration_denied(client, session):
    await seed(session, mode="live")
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 422
    response = await client.post("/api/v1/research/trials", headers=READONLY, json=registration())
    assert response.status_code == 403


def cost(**extra):
    ts = (NOW - timedelta(hours=1)).isoformat()
    return {
        "reference": "invoice-1",
        "kind": "cost",
        "amount_eur": "25.00",
        "period_start": ts,
        "period_end": ts,
        "note": "Actual model research expense",
        **extra,
    }


async def test_costs_are_admin_only_idempotent_and_budget_is_not_assumed_spend(client):
    path = "/api/v1/research/costs"
    assert (await client.post(path, headers=RESEARCH, json=cost())).status_code == 403
    assert (await client.post(path, headers=ADMIN, json=cost())).status_code == 201
    assert (await client.post(path, headers=ADMIN, json=cost())).status_code == 409
    budget = (await client.get("/api/v1/research/budget", headers=RESEARCH)).json()
    assert budget["recorded_spend_eur"] == 25
    assert budget["remaining_eur"] == 75
    assert budget["coverage_complete"] is False
    assert budget["provider_spend_enforced"] is False
    coverage = cost(
        reference="coverage-1",
        kind="coverage",
        amount_eur="0",
        period_start=NOW.replace(day=1).isoformat(),
        period_end=NOW.isoformat(),
    )
    # Coverage must begin at midnight for the whole calendar month.
    coverage["period_start"] = NOW.replace(day=1, hour=0).isoformat()
    assert (await client.post(path, headers=ADMIN, json=coverage)).status_code == 201
    budget = (await client.get("/api/v1/research/budget", headers=RESEARCH)).json()
    assert budget["coverage_complete"] is True
    assert (
        await client.post(path, headers=ADMIN, json=cost(reference="invoice-2", amount_eur=90))
    ).status_code == 201
    budget = (await client.get("/api/v1/research/budget", headers=RESEARCH)).json()
    assert budget["over_budget"] is True
    ledger = (await client.get(path, headers=READONLY)).json()
    assert len(ledger) == 3
    assert {r["reference"] for r in ledger} == {"invoice-1", "invoice-2", "coverage-1"}


async def test_cannot_attest_future_costs_or_supply_naive_dates(client):
    future = (NOW + timedelta(days=1)).isoformat()
    assert (
        await client.post(
            "/api/v1/research/costs", headers=ADMIN, json=cost(period_start=future, period_end=future)
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/research/costs",
            headers=ADMIN,
            json=cost(period_start="2026-09-16", period_end="2026-09-16"),
        )
    ).status_code == 422


async def test_pending_order_prevents_registration(client, session):
    await seed(session)
    session.add(
        OrderRow(
            id="pending",
            run_id="root",
            ts=NOW,
            pair="BTC/EUR",
            side="buy",
            type="market",
            size=1,
            status="open",
        )
    )
    await session.commit()
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 422
    assert "first order" in response.json()["detail"]


async def test_audit_halt_reaches_trial_report(client, session, monkeypatch):
    await seed(session)
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    trial_id = response.json()["id"]
    session.add(
        EventRow(
            id="halt",
            ts=NOW + timedelta(hours=1),
            source="engine",
            level="warn",
            message="daily loss halt",
            data={"run_id": "root", "halt_reason": "daily loss"},
        )
    )
    await session.commit()
    monkeypatch.setattr(research, "utc_now", lambda: NOW + timedelta(days=1))
    result = (await client.get(f"/api/v1/research/trials/{trial_id}", headers=READONLY)).json()
    assert result["status"] == "invalidated"
    assert "run halted during evaluation" in result["reasons"]


async def test_research_capacity_is_bounded(session):
    from fastapi import HTTPException

    from kaupo.api.research_limits import check_research_capacity
    from kaupo.db.models import BacktestJobRow

    for i in range(6):
        session.add(BacktestJobRow(id=f"job{i}", created_at=NOW, updated_at=NOW, status="queued", payload={}))
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await check_research_capacity(session, backtest=True)
    assert exc.value.status_code == 429
    for i in range(12):
        await create_assignment(
            session,
            id=f"slot{i}",
            strategy_id="trend",
            pair="BTC/EUR",
            timeframe="1d",
            mode="shadow",
            params={},
            enabled=True,
            starting_cash=10000,
        )
    with pytest.raises(HTTPException) as exc:
        await check_research_capacity(session, backtest=False)
    assert exc.value.status_code == 429


async def test_new_trial_assignment_does_not_supersede_existing_candidate(session):
    from kaupo.core.recorder import DbRecorder, RunInfo, supersede_stale_runs
    from kaupo.db.session import get_sessionmaker
    from kaupo.domain import RunMode

    old = await seed(session)
    # A fresh candidate with the same strategy, pair and timeframe must not
    # change the existing assignment's run during either resume or recording.
    await supersede_stale_runs(
        session,
        mode=RunMode.SHADOW,
        strategy_id="trend",
        pair="BTC/EUR",
        timeframe="1d",
        assignment_id="new-candidate",
    )
    await session.commit()
    await session.refresh(old)
    assert old.status == "running"
    recorder = DbRecorder(get_sessionmaker())
    await recorder.start(
        RunInfo(
            mode=RunMode.SHADOW,
            strategy_id="trend",
            strategy_version="v1",
            strategy_source_hash="v1",
            config={**old.config, "assignment_id": "new-candidate"},
        )
    )
    await session.refresh(old)
    assert old.status == "running"
