"""API contract tests against a real Postgres (httpx ASGI transport, no server)."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.config import get_settings
from kaupo.data.candles import upsert_candles
from kaupo.db.session import dispose_engine, get_sessionmaker
from kaupo.domain import Candle, Pair, RunId, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    # auth disabled for these tests
    os.environ.pop("KAUPO_ADMIN_TOKEN", None)
    os.environ.pop("KAUPO_READONLY_TOKEN", None)
    get_settings.cache_clear()
    await dispose_engine()

    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _seed_run(session: AsyncSession) -> RunId:
    """A real backtest run with orders, fills, and equity snapshots."""
    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(12)
    ]
    await upsert_candles(session, candles)
    await session.commit()

    strategy = load_strategies("examples/strategies")["regime-switch"]
    run_id, _, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
        ),
        get_sessionmaker(),
    )
    return run_id


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_status(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_run(session)
    r = await client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "BTC/EUR/1h" in body["candles"]


async def test_runs_endpoints(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _seed_run(session)

    r = await client.get("/api/v1/runs", params={"mode": "backtest"})
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["strategy_id"] == "regime-switch"
    assert runs[0]["metrics"]["status"] == "completed"

    r = await client.get(f"/api/v1/runs/{run_id}")
    assert r.status_code == 200

    r = await client.get(f"/api/v1/runs/{run_id}/equity")
    assert r.status_code == 200
    equity = r.json()
    assert len(equity) == 12
    assert equity[0]["equity"] == 10_000.0

    r = await client.get(f"/api/v1/runs/{run_id}/orders")
    assert r.status_code == 200

    r = await client.get(f"/api/v1/runs/{run_id}/trades")
    assert r.status_code == 200

    r = await client.get(f"/api/v1/runs/{run_id}/positions")
    assert r.status_code == 200

    r = await client.get("/api/v1/runs/does-not-exist")
    assert r.status_code == 404


async def test_backtest_job(client: AsyncClient, session: AsyncSession) -> None:
    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(120)
    ]
    await upsert_candles(session, candles)
    await session.commit()

    r = await client.post(
        "/api/v1/backtests",
        json={
            "strategy": "regime-switch",
            "pair": "BTC/EUR",
            "timeframe": "1h",
            "start": BASE.isoformat(),
            "end": (BASE + timedelta(hours=120)).isoformat(),
        },
    )
    assert r.status_code == 202
    job_id = r.json()["run_id"]

    for _ in range(100):
        r = await client.get(f"/api/v1/backtests/{job_id}")
        if r.json()["status"] != "running":
            break
    assert r.json()["status"] == "completed"
    assert r.json()["run"]["metrics"]["num_fills"] >= 0

    r = await client.post("/api/v1/backtests", json={"strategy": "nope", "pair": "BTC/EUR"})
    assert r.status_code == 404


async def test_candles_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(5)
    ]
    await upsert_candles(session, candles)
    await session.commit()

    r = await client.get(
        "/api/v1/candles",
        params={
            "pair": "BTC/EUR",
            "timeframe": "1h",
            "start": BASE.isoformat(),
            "end": (BASE + timedelta(hours=5)).isoformat(),
        },
    )
    assert r.status_code == 200
    assert len(r.json()) == 5
    assert r.json()[0]["close"] == 100.0


async def test_control_writes_command_events(client: AsyncClient, session: AsyncSession) -> None:
    from sqlalchemy import select

    from kaupo.db.models import EventRow

    r = await client.post("/api/v1/control/kill", json={"run_id": "abc"})
    assert r.status_code == 200
    assert r.json()["command"] == "kill"

    rows = (await session.execute(select(EventRow).where(EventRow.source == "control"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].data == {"command": "kill", "run_id": "abc"}

    r = await client.post("/api/v1/control/nonsense", json={})
    assert r.status_code == 400

    r = await client.get("/api/v1/events")
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_daily_report(client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, RunRow
    from kaupo.domain import new_id, utc_now

    # synthetic shadow run with equity snapshots and fills *today* (real time)
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="shadow",
            strategy_id="regime-switch",
            strategy_version="v1",
            started_at=today - timedelta(days=1),
            status="running",
            config={"pair": "BTC/EUR", "timeframe": "1h"},
        )
    )
    await session.flush()  # runs row must exist before dependent rows (FK)
    for oid, side in (("o1", "buy"), ("o2", "sell")):
        session.add(
            OrderRow(
                id=oid,
                run_id=run_id,
                ts=today,
                pair="BTC/EUR",
                side=side,
                type="market",
                size=1.0,
                status="filled",
            )
        )
    await session.flush()
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id=run_id,
            ts=today - timedelta(hours=1),
            equity=10_000,
            cash=10_000,
            unrealized_pnl=0,
        )
    )
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id=run_id,
            ts=today + timedelta(hours=3),
            equity=10_150,
            cash=10_150,
            unrealized_pnl=0,
        )
    )
    session.add(
        FillRow(
            id=new_id(),
            order_id="o1",
            run_id=run_id,
            ts=today + timedelta(hours=1),
            pair="BTC/EUR",
            side="buy",
            price=100,
            size=1.0,
            fee=0.26,
        )
    )
    session.add(
        FillRow(
            id=new_id(),
            order_id="o2",
            run_id=run_id,
            ts=today + timedelta(hours=2),
            pair="BTC/EUR",
            side="sell",
            price=101.5,
            size=1.0,
            fee=0.26,
        )
    )
    await session.commit()

    day_str = today.date().isoformat()
    r = await client.get("/api/v1/reports/daily", params={"day": day_str})
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == day_str
    assert body["totals"]["num_runs"] == 1
    run = body["runs"][0]
    assert run["active"] is True
    assert run["start_equity"] == 10_000.0
    assert run["end_equity"] == 10_150.0
    assert run["pnl"] == 150.0
    assert run["num_fills"] == 2
    assert run["round_trips"] == 1
    assert run["winning_trips"] == 1
    assert body["totals"]["total_pnl"] == 150.0

    # stored in reports table (idempotent: one row per period)
    from sqlalchemy import select

    from kaupo.db.models import ReportRow

    rows = (await session.execute(select(ReportRow))).scalars().all()
    assert len(rows) == 1
    r = await client.get("/api/v1/reports/daily", params={"day": day_str})
    rows = (await session.execute(select(ReportRow))).scalars().all()
    assert len(rows) == 1


async def test_strategies_endpoint(client: AsyncClient) -> None:
    r = await client.get("/api/v1/strategies")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "regime-switch"
    assert body[0]["params_schema"]["type"] == "object"
