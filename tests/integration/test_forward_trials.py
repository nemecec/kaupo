"""Forward registration, immutable contracts, and operator cost accounting."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from kaupo.api.routes import research
from kaupo.config import get_settings
from kaupo.core.provenance import engine_version
from kaupo.data.assignments import create_assignment
from kaupo.db.models import EquitySnapshotRow, EventRow, FillRow, OrderRow, RunRow

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


def reference_config(days=365, **overrides):
    start = NOW - timedelta(days=days + 2)
    return {
        "pair": "BTC/EUR",
        "timeframe": "1d",
        "exchange": "kraken",
        "instrument": "spot",
        "params": {},
        "start": start.isoformat(),
        "end": (start + timedelta(days=days)).isoformat(),
        "starting_cash": 10000,
        "fees": {"taker_bps": 80, "maker_bps": 40, "slippage_bps": 5, "marketable_limit": "skip"},
        # as a backtest stores it: the venue's rates already folded in
        "risk": {"max_position_quote": 1000, "taker_fee_bps": 80, "slippage_bps": 5},
        "lookback": 300,
        "liquidate_end": True,
        **overrides,
    }


async def seed_reference(
    session,
    run_id="reference",
    days=365,
    positions=40,
    size=1.0,
    status="completed",
    metrics=None,
    gap_days=0,
    strategy_id="trend",
    **config_overrides,
):
    """A completed spot backtest that can size a forward trial.

    ``days`` of daily equity, ``positions`` flat-to-flat round trips of
    ``size`` at 1000 EUR. The defaults (365 days, 40 positions) imply the
    365-day floor; fewer positions imply a longer window.
    """
    config = reference_config(days=days, **config_overrides)
    start = datetime.fromisoformat(config["start"])
    session.add(
        RunRow(
            id=run_id,
            mode="backtest",
            strategy_id=strategy_id,
            strategy_version="v1",
            status=status,
            started_at=start,
            ended_at=start + timedelta(days=days),
            config=config,
            metrics=metrics,
        )
    )
    await session.flush()  # no mapper relationship orders the run before its rows
    hole = range(days // 4, days // 4 + gap_days)
    for i in range(days):
        if i in hole:
            continue
        session.add(
            EquitySnapshotRow(
                id=f"{run_id}-e{i}",
                run_id=run_id,
                ts=start + timedelta(days=i),
                equity=10000 + i,
                cash=10000,
                unrealized_pnl=0,
            )
        )
    stride = max(2, (days - 2) // max(positions, 1))
    trades = [
        (f"{run_id}-{side}{j}", side, start + timedelta(days=j * stride + offset))
        for j in range(positions)
        for offset, side in ((0, "buy"), (1, "sell"))
    ]
    for trade_id, side, ts in trades:
        session.add(
            OrderRow(
                id=trade_id,
                run_id=run_id,
                ts=ts,
                pair="BTC/EUR",
                side=side,
                type="market",
                size=size,
                status="filled",
            )
        )
    await session.flush()  # fills reference their order row
    for trade_id, side, ts in trades:
        session.add(
            FillRow(
                id=trade_id,
                order_id=trade_id,
                run_id=run_id,
                ts=ts,
                pair="BTC/EUR",
                side=side,
                price=1000,
                size=size,
                fee=0.8,
            )
        )
    await session.commit()


async def seed(session, mode="shadow", strategy_version="v1", **overrides):
    await seed_reference(session)
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
        strategy_version=strategy_version,
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
            "fees": {"maker_bps": 40, "taker_bps": 80, "slippage_bps": 5, "marketable_limit": "skip"},
            # as a shadow run stores it: the venue's rates not yet folded in
            "risk": {"max_position_quote": 1000, "taker_fee_bps": 80, "slippage_bps": 5},
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
        "reference_run_id": "reference",
        **extra,
    }


async def test_trial_is_prospective_immutable_and_research_readable(client, session):
    await seed(session)
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 201, response.text
    trial = response.json()
    assert trial["registered_at"] == NOW.isoformat()
    assert trial["ends_at"] == (NOW + timedelta(days=365)).isoformat()
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


async def test_registration_fixes_a_planned_version_2_window(client, session):
    await seed(session)
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 201, response.text
    policy = response.json()["policy"]
    assert policy["version"] == 2
    assert policy["evaluation_days"] == 365  # the floor; the rate implies 274 days
    plan = policy["plan"]
    assert plan["reference_run_id"] == "reference"
    assert plan["reference_completed_positions"] == 40
    assert round(plan["positions_per_year"]) == 40
    assert plan["required_horizon_days"] == 274
    assert plan["margin"] == 1.5
    assert plan["blockers"] == []


async def test_horizon_follows_the_reference_trade_rate(client, session):
    """A strategy that trades a quarter as often gets a window three times as long."""
    await seed(session)
    await seed_reference(session, run_id="slow", positions=10)
    response = await client.post(
        "/api/v1/research/trials", headers=RESEARCH, json=registration(reference_run_id="slow")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["policy"]["evaluation_days"] == 1095
    assert body["ends_at"] == (NOW + timedelta(days=1095)).isoformat()


async def test_registration_requires_a_planning_reference(client, session):
    await seed(session)
    body = registration()
    del body["reference_run_id"]
    assert (await client.post("/api/v1/research/trials", headers=RESEARCH, json=body)).status_code == 422
    missing = registration(reference_run_id="nosuchrun")
    assert (await client.post("/api/v1/research/trials", headers=RESEARCH, json=missing)).status_code == 404


async def test_reference_for_another_configuration_cannot_size_the_trial(client, session):
    await seed(session)
    await seed_reference(session, run_id="other", strategy_id="momentum")
    response = await client.post(
        "/api/v1/research/trials", headers=RESEARCH, json=registration(reference_run_id="other")
    )
    assert response.status_code == 422
    assert "different strategy id" in response.json()["detail"]


@pytest.mark.parametrize(
    "reference,shadow,expected",
    [
        # same strategy id and parameters, different code behind the name
        ({"strategy_version": "v2"}, {}, "different strategy source version"),
        ({"behaviour_hash": "other-behaviour"}, {}, "different strategy behaviour"),
        # the run executes code the reference never measured
        ({}, {"engine_version": "another-build"}, "current engine version"),
        ({"engine_version": "older-build"}, {}, "different engine version"),
        # the run pays more than the reference did, so it will trade less often
        (
            {},
            {"fees": {"maker_bps": 80, "taker_bps": 160, "slippage_bps": 20, "marketable_limit": "skip"}},
            "priced taker_bps below this run",
        ),
        # the run can hold four times the position, so it clears the gate sooner
        (
            {},
            {"risk": {"max_position_quote": 4000, "taker_fee_bps": 80, "slippage_bps": 5}},
            "different effective risk limits",
        ),
    ],
)
async def test_a_reference_that_does_not_describe_the_run_is_rejected(
    client, session, reference, shadow, expected
):
    await seed(session, **shadow)
    if reference:
        run = await session.get(RunRow, "reference")
        for key, value in reference.items():
            if hasattr(run, key):
                setattr(run, key, value)
            else:
                run.config = {**run.config, key: value}
        await session.commit()
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 422, response.text
    assert expected in response.json()["detail"]
    assert (await client.get("/api/v1/research/trials", headers=RESEARCH)).json() == []


async def test_a_reference_without_a_requested_window_cannot_size_the_trial(client, session):
    await seed(session)
    run = await session.get(RunRow, "reference")
    run.config = {k: v for k, v in run.config.items() if k != "end"}
    await session.commit()
    response = await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())
    assert response.status_code == 422, response.text
    assert "does not record the date window it requested" in response.json()["detail"]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"status": "failed"}, "did not complete"),
        ({"metrics": {"halt_reason": "daily loss"}}, "halted"),
        ({"instrument": "perp"}, "spot backtest"),
        ({"sweep": {"group": "g", "point": 3}}, "sweep slice"),
        ({"fees": {"taker_bps": 10, "maker_bps": 5, "marketable_limit": "skip"}}, "below the current"),
        ({"fees": {"taker_bps": 80, "maker_bps": 40, "marketable_limit": "maker"}}, "live-mirror"),
        ({"starting_cash": 50000}, "10000 EUR baseline"),
        ({"days": 120}, "estimate a trade rate"),
        ({"gap_days": 120}, "gaps in its equity record"),
        ({"positions": 4}, "at least 5 are needed"),
    ],
)
async def test_unusable_reference_is_rejected_with_its_reason(client, session, kwargs, expected):
    await seed(session)
    await seed_reference(session, run_id="bad", **kwargs)
    response = await client.post(
        "/api/v1/research/trials", headers=RESEARCH, json=registration(reference_run_id="bad")
    )
    assert response.status_code == 422, response.text
    assert expected in response.json()["detail"]


async def test_a_strategy_too_slow_for_the_cap_is_rejected_not_shortened(client, session):
    """Five positions a year cannot reach twenty inside the cap. Say so.

    The rejection reports unreachable evidence. It must not suggest larger
    positions, which would add risk to satisfy the gate.
    """
    await seed(session)
    await seed_reference(session, run_id="glacial", positions=5)
    response = await client.post(
        "/api/v1/research/trials", headers=RESEARCH, json=registration(reference_run_id="glacial")
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "enough forward evidence in a testable horizon" in detail
    assert "larger position" not in detail
    assert (await client.get("/api/v1/research/trials", headers=RESEARCH)).json() == []


async def test_reference_trades_never_become_forward_evidence(client, session):
    await seed(session)
    trial = (await client.post("/api/v1/research/trials", headers=RESEARCH, json=registration())).json()
    report = (await client.get(f"/api/v1/research/trials/{trial['id']}", headers=READONLY)).json()
    assert report["completed_positions"] == 0  # the reference's 40 round trips do not count
    assert report["status"] == "collecting"
    assert report["automatic_live_promotion"] is False


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
    assert budget["remaining_eur"] is None
    assert budget["actual_spend_eur"] is None
    assert budget["recorded_allowance_eur"] == 75
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
