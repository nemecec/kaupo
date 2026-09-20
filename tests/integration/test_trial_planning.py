"""The advisory trial planner: GET /api/v1/research/trial-plan.

The planner reads a completed backtest and reports the window a forward
trial on that configuration needs. It writes nothing, and registration
applies the same rules, so the answer cannot drift from the contract.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from kaupo.api.routes import research
from kaupo.config import get_settings
from tests.integration.test_forward_trials import NOW, registration, seed, seed_reference

pytestmark = pytest.mark.integration
RESEARCH = {"Authorization": "Bearer research-test"}
READONLY = {"Authorization": "Bearer readonly-test"}
PLAN = "/api/v1/research/trial-plan"


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


async def test_plan_reports_the_window_the_reference_implies(client, session):
    await seed_reference(session, positions=10)
    response = await client.get(PLAN, headers=READONLY, params={"reference_run_id": "reference"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy_version"] == 2
    assert body["min_completed_positions"] == 20
    plan = body["plan"]
    assert plan["usable"] is True
    assert plan["blockers"] == []
    assert plan["horizon_days"] == 1095
    assert plan["window_days"] == 365
    assert plan["reference_completed_positions"] == 10
    assert plan["margin"] == 1.5
    assert plan["max_horizon_days"] == 1825


async def test_plan_reports_why_a_reference_cannot_be_used(client, session):
    await seed_reference(session, sweep={"group": "g", "point": 4})
    body = (await client.get(PLAN, headers=RESEARCH, params={"reference_run_id": "reference"})).json()
    assert body["plan"]["usable"] is False
    assert any("sweep slice" in reason for reason in body["plan"]["blockers"])


async def test_plan_needs_an_existing_reference(client, session):
    await seed_reference(session)
    assert (await client.get(PLAN, headers=READONLY)).status_code == 422
    missing = await client.get(PLAN, headers=READONLY, params={"reference_run_id": "nope"})
    assert missing.status_code == 404


async def test_plan_writes_nothing(client, session):
    await seed(session)
    before = (await client.get("/api/v1/research/trials", headers=RESEARCH)).json()
    plan = await client.get(PLAN, headers=RESEARCH, params={"reference_run_id": "reference"})
    assert plan.status_code == 200
    assert (await client.get("/api/v1/research/trials", headers=RESEARCH)).json() == before == []


async def test_registration_records_the_window_the_planner_reported(client, session):
    await seed(session)
    await seed_reference(session, run_id="slow", positions=10)
    advised = (await client.get(PLAN, headers=RESEARCH, params={"reference_run_id": "slow"})).json()["plan"]
    registered = await client.post(
        "/api/v1/research/trials", headers=RESEARCH, json=registration(reference_run_id="slow")
    )
    assert registered.status_code == 201, registered.text
    policy = registered.json()["policy"]
    assert policy["version"] == 2
    assert policy["evaluation_days"] == advised["horizon_days"]
    assert policy["plan"] == advised  # the rationale is stored, not recomputed later
