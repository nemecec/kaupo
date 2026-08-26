"""API contract tests against a real Postgres (httpx ASGI transport, no server)."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


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

    strategy = load_strategies(EXAMPLES_DIR)["regime-switch"]
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


async def test_backtest_job_failure_surfaces_message(client: AsyncClient) -> None:
    # no candles ingested for this pair: the job error must carry the reason,
    # so a data gap is distinguishable from a strategy bug
    r = await client.post(
        "/api/v1/backtests",
        json={
            "strategy": "regime-switch",
            "pair": "ETH/EUR",
            "timeframe": "1h",
            "start": BASE.isoformat(),
            "end": (BASE + timedelta(hours=48)).isoformat(),
        },
    )
    assert r.status_code == 202
    job_id = r.json()["run_id"]

    for _ in range(100):
        r = await client.get(f"/api/v1/backtests/{job_id}")
        if r.json()["status"] != "running":
            break
    assert r.json()["status"] == "failed"
    assert r.json()["error"].startswith("ValueError: No kraken candles for ETH/EUR 1h in range")


async def test_backtest_job_portfolio_pairs(client: AsyncClient, session: AsyncSession) -> None:
    for j, pair in enumerate(("ADA/EUR", "BTC/EUR", "SOL/EUR")):
        candles = [
            Candle(
                pair=Pair.parse(pair),
                timeframe=Timeframe.H1,
                ts=BASE + timedelta(hours=i),
                open=100 * (j + 1) + i,
                high=100 * (j + 1) + i + 1,
                low=100 * (j + 1) + i - 1,
                close=100 * (j + 1) + i,
                volume=1.0,
            )
            for i in range(120)
        ]
        await upsert_candles(session, candles)
    await session.commit()

    r = await client.post(
        "/api/v1/backtests",
        json={
            "strategy": "momentum-rotation",
            "pairs": ["BTC/EUR", "SOL/EUR", "ADA/EUR"],
            "timeframe": "1h",
            "start": BASE.isoformat(),
            "end": (BASE + timedelta(hours=120)).isoformat(),
            "params": {"lookback": 24, "top_k": 2, "rebalance_interval": 24},
        },
    )
    assert r.status_code == 202
    job_id = r.json()["run_id"]

    for _ in range(100):
        r = await client.get(f"/api/v1/backtests/{job_id}")
        if r.json()["status"] != "running":
            break
    assert r.json()["status"] == "completed"
    metrics = r.json()["run"]["metrics"]
    assert metrics["universe"] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]
    assert set(metrics["per_pair"]) == {"ADA/EUR", "BTC/EUR", "SOL/EUR"}
    assert r.json()["run"]["config"]["pair"] == "ADA/EUR,BTC/EUR,SOL/EUR"


async def test_backtest_pairs_routing_validation(client: AsyncClient) -> None:
    # pair and pairs together
    r = await client.post(
        "/api/v1/backtests",
        json={"strategy": "regime-switch", "pair": "BTC/EUR", "pairs": ["BTC/EUR", "SOL/EUR"]},
    )
    assert r.status_code == 422
    # a one-entry universe
    r = await client.post("/api/v1/backtests", json={"strategy": "momentum-rotation", "pairs": ["BTC/EUR"]})
    assert r.status_code == 422
    # a single-pair strategy does not take pairs
    r = await client.post(
        "/api/v1/backtests", json={"strategy": "regime-switch", "pairs": ["BTC/EUR", "SOL/EUR"]}
    )
    assert r.status_code == 422
    assert "not a portfolio strategy" in r.json()["detail"]
    # a portfolio strategy does not take pair
    r = await client.post("/api/v1/backtests", json={"strategy": "momentum-rotation", "pair": "BTC/EUR"})
    assert r.status_code == 422
    assert "portfolio strategy" in r.json()["detail"]
    # mixed quotes
    r = await client.post(
        "/api/v1/backtests", json={"strategy": "momentum-rotation", "pairs": ["BTC/EUR", "SOL/USD"]}
    )
    assert r.status_code == 422
    assert "one quote currency" in r.json()["detail"]


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

    r = await client.post("/api/v1/control/kill", json={"run_id": None})
    assert r.status_code == 200
    assert r.json()["command"] == "kill"

    rows = (await session.execute(select(EventRow).where(EventRow.source == "control"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].data == {"command": "kill", "run_id": None}

    r = await client.post("/api/v1/control/nonsense", json={})
    assert r.status_code == 400

    # nonexistent run id -> 404 (was a silent no-op)
    r = await client.post("/api/v1/control/kill", json={"run_id": "does-not-exist"})
    assert r.status_code == 404

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
    assert {s["id"] for s in body} == {"momentum-rotation", "regime-switch"}
    assert body[0]["params_schema"]["type"] == "object"


async def test_daily_report_overnight_round_trip_and_ended_runs(
    client: AsyncClient, session: AsyncSession
) -> None:
    from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, RunRow
    from kaupo.domain import new_id, utc_now

    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    # run that bought yesterday and sells today -> trip must count today
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="shadow",
            strategy_id="s",
            strategy_version="v",
            started_at=yesterday,
            status="running",
            config={"pair": "BTC/EUR", "timeframe": "1h"},
        )
    )
    session.add(
        RunRow(
            id=new_id(),
            mode="shadow",
            strategy_id="s",
            strategy_version="v",
            started_at=today - timedelta(days=10),
            ended_at=today - timedelta(days=5),
            status="completed",
            config={"pair": "BTC/EUR", "timeframe": "1h"},
        )
    )
    await session.flush()
    for oid, side, ts, _price in (
        ("o1", "buy", yesterday + timedelta(hours=23), 100.0),
        ("o2", "sell", today + timedelta(hours=1), 110.0),
    ):
        session.add(
            OrderRow(
                id=oid,
                run_id=run_id,
                ts=ts,
                pair="BTC/EUR",
                side=side,
                type="market",
                size=1.0,
                status="filled",
            )
        )
    await session.flush()
    session.add(
        FillRow(
            id=new_id(),
            order_id="o1",
            run_id=run_id,
            ts=yesterday + timedelta(hours=23),
            pair="BTC/EUR",
            side="buy",
            price=100.0,
            size=1.0,
            fee=0.0,
        )
    )
    session.add(
        FillRow(
            id=new_id(),
            order_id="o2",
            run_id=run_id,
            ts=today + timedelta(hours=1),
            pair="BTC/EUR",
            side="sell",
            price=110.0,
            size=1.0,
            fee=0.0,
        )
    )
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id=run_id,
            ts=today + timedelta(hours=2),
            equity=10_010,
            cash=10_010,
            unrealized_pnl=0,
        )
    )
    await session.commit()

    day_str = today.date().isoformat()
    r = await client.get("/api/v1/reports/daily", params={"day": day_str})
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["num_runs"] == 1  # the long-dead run is excluded
    run = body["runs"][0]
    assert run["round_trips"] == 1  # buy yesterday, sell today — counts
    assert run["winning_trips"] == 1


async def test_zombie_run_reported_inactive(client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.db.models import RunRow
    from kaupo.domain import new_id, utc_now

    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    # status=running but no activity today (e.g. container-killed)
    session.add(
        RunRow(
            id=new_id(),
            mode="shadow",
            strategy_id="s",
            strategy_version="v",
            started_at=today - timedelta(days=1),
            status="running",
            config={"pair": "BTC/EUR", "timeframe": "1h"},
        )
    )
    await session.commit()

    r = await client.get("/api/v1/reports/daily", params={"day": today.date().isoformat()})
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["num_runs"] == 1
    assert body["totals"]["active_runs"] == 0
    assert body["runs"][0]["active"] is False


async def test_backtest_lint_enforced(client: AsyncClient, session: AsyncSession, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "bad.py").write_text("import requests\n")
    os.environ["KAUPO_STRATEGIES_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    try:
        r = await client.post("/api/v1/backtests", json={"strategy": "bad", "pair": "BTC/EUR"})
        assert r.status_code == 422
        assert "violations" in r.json()["detail"]["error"]
    finally:
        os.environ.pop("KAUPO_STRATEGIES_DIR", None)
        get_settings.cache_clear()


async def test_equity_endpoint_returns_latest_n(client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.db.models import EquitySnapshotRow, RunRow
    from kaupo.domain import new_id, utc_now

    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="backtest",
            strategy_id="s",
            strategy_version="v",
            started_at=today,
            status="completed",
            config={},
        )
    )
    await session.flush()
    for i in range(10):
        session.add(
            EquitySnapshotRow(
                id=new_id(),
                run_id=run_id,
                ts=today + timedelta(hours=i),
                equity=1000 + i,
                cash=1000 + i,
                unrealized_pnl=0,
            )
        )
    await session.commit()

    r = await client.get(f"/api/v1/runs/{run_id}/equity", params={"limit": 3})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert [p["equity"] for p in body] == [1007.0, 1008.0, 1009.0]  # latest 3, ascending


async def test_report_first_day_baseline_uses_starting_cash(
    client: AsyncClient, session: AsyncSession
) -> None:
    from kaupo.db.models import EquitySnapshotRow, RunRow
    from kaupo.domain import new_id, utc_now

    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="shadow",
            strategy_id="s",
            strategy_version="v",
            started_at=today,
            status="running",
            config={"pair": "BTC/EUR", "timeframe": "1h", "starting_cash": 5000.0},
        )
    )
    await session.flush()
    # first in-day snapshot is already post-trade (equity 5050)
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id=run_id,
            ts=today + timedelta(hours=1),
            equity=5050.0,
            cash=5050.0,
            unrealized_pnl=0,
        )
    )
    await session.commit()

    r = await client.get("/api/v1/reports/daily", params={"day": today.date().isoformat()})
    run = r.json()["runs"][0]
    assert run["start_equity"] == 5000.0  # starting_cash, not the first snapshot
    assert run["pnl"] == 50.0


async def test_positions_marks_at_run_timeline(client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.db.models import CandleRow, EquitySnapshotRow, FillRow, OrderRow, RunRow
    from kaupo.domain import new_id

    base = datetime(2026, 1, 1, tzinfo=UTC)
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="backtest",
            strategy_id="s",
            strategy_version="v",
            started_at=base,
            ended_at=base + timedelta(hours=10),
            status="completed",
            config={"pair": "BTC/EUR", "timeframe": "1h"},
        )
    )
    await session.flush()
    session.add(
        OrderRow(
            id="o1",
            run_id=run_id,
            ts=base,
            pair="BTC/EUR",
            side="buy",
            type="market",
            size=1.0,
            status="filled",
        )
    )
    await session.flush()
    session.add(
        FillRow(
            id=new_id(),
            order_id="o1",
            run_id=run_id,
            ts=base,
            pair="BTC/EUR",
            side="buy",
            price=100.0,
            size=1.0,
            fee=0.0,
        )
    )
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id=run_id,
            ts=base + timedelta(hours=10),
            equity=10_100,
            cash=9_000,
            unrealized_pnl=100,
        )
    )
    await session.commit()

    # candles AFTER the run's timeline (simulated period) at a very different price
    for i in range(5):
        session.add(
            CandleRow(
                pair="BTC/EUR",
                timeframe="1h",
                ts=base + timedelta(days=30, hours=i),
                open=5000,
                high=5100,
                low=4900,
                close=5050,
                volume=1,
            )
        )
    # and one within the period
    session.add(
        CandleRow(
            pair="BTC/EUR",
            timeframe="1h",
            ts=base + timedelta(hours=9),
            open=110,
            high=112,
            low=109,
            close=111,
            volume=1,
        )
    )
    await session.commit()

    r = await client.get(f"/api/v1/runs/{run_id}/positions")
    assert r.status_code == 200
    positions = r.json()
    assert len(positions) == 1
    assert positions[0]["last_price"] == 111.0  # not 5050 from after the run
    assert positions[0]["avg_entry"] == 100.0
