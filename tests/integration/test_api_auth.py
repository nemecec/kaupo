"""Auth behavior and the DB control probe."""

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import get_settings
from kaupo.core.runner import DbControlProbe
from kaupo.db.models import EventRow
from kaupo.db.session import dispose_engine, get_sessionmaker
from kaupo.domain import new_id, utc_now

pytestmark = pytest.mark.integration


@pytest.fixture
async def authed_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    os.environ["KAUPO_ADMIN_TOKEN"] = "admin-secret"  # noqa: S105 (test token)
    os.environ["KAUPO_READONLY_TOKEN"] = "readonly-secret"  # noqa: S105 (test token)
    os.environ["KAUPO_RESEARCH_TOKEN"] = "research-secret"  # noqa: S105 (test token)
    get_settings.cache_clear()
    await dispose_engine()
    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    os.environ.pop("KAUPO_ADMIN_TOKEN", None)
    os.environ.pop("KAUPO_READONLY_TOKEN", None)
    os.environ.pop("KAUPO_RESEARCH_TOKEN", None)
    get_settings.cache_clear()
    await dispose_engine()


ADMIN = {"Authorization": "Bearer admin-secret"}
READONLY = {"Authorization": "Bearer readonly-secret"}
RESEARCH = {"Authorization": "Bearer research-secret"}


async def _seed_assignment(session: AsyncSession, assignment_id: str, mode: str) -> None:
    from kaupo.data import assignments as assignments_repo

    await assignments_repo.create_assignment(
        session,
        id=assignment_id,
        strategy_id="regime-switch",
        pair="SOL/EUR",
        timeframe="4h",
        mode=mode,
        params={},
        enabled=True,
    )
    await session.commit()


async def _assignment(client: AsyncClient, assignment_id: str) -> dict[str, object]:
    rows = (await client.get("/api/v1/assignments", headers=ADMIN)).json()
    return next(row for row in rows if row["id"] == assignment_id)


async def test_no_token_401(authed_client: AsyncClient) -> None:
    r = await authed_client.get("/api/v1/runs")
    assert r.status_code == 401


async def test_wrong_token_401(authed_client: AsyncClient) -> None:
    r = await authed_client.get("/api/v1/runs", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_readonly_can_get_not_post(authed_client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer readonly-secret"}
    r = await authed_client.get("/api/v1/runs", headers=headers)
    assert r.status_code == 200

    r = await authed_client.post("/api/v1/control/kill", json={}, headers=headers)
    assert r.status_code == 403

    r = await authed_client.post("/api/v1/backtests", json={}, headers=headers)
    assert r.status_code == 403


async def test_readonly_can_get_trades(authed_client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.data.trades import upsert_trade_ticks
    from kaupo.domain import TradeTick

    now = utc_now()
    ticks = [
        TradeTick(
            exchange="kraken",
            pair="BTC/EUR",
            ts=now - timedelta(minutes=i + 1),
            price=100.0 + i,
            size=0.1,
            side="buy",
        )
        for i in range(3)
    ]
    await upsert_trade_ticks(session, ticks)
    await session.commit()

    headers = {"Authorization": "Bearer readonly-secret"}
    r = await authed_client.get(
        "/api/v1/trades",
        params={
            "pair": "BTC/EUR",
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": now.isoformat(),
        },
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert body[0]["pair"] == "BTC/EUR"
    assert body[0]["side"] == "buy"


async def test_readonly_can_get_book(authed_client: AsyncClient, session: AsyncSession) -> None:
    from kaupo.data.book import upsert_book_snapshots
    from kaupo.domain import BookSnapshot

    now = utc_now()
    snapshots = [
        BookSnapshot(
            exchange="kraken",
            pair="BTC/EUR",
            ts=now - timedelta(minutes=i + 1),
            bid=100.0 + i,
            ask=100.5 + i,
            bid_size=1.0,
            ask_size=2.0,
        )
        for i in range(3)
    ]
    await upsert_book_snapshots(session, snapshots)
    await session.commit()

    headers = {"Authorization": "Bearer readonly-secret"}
    r = await authed_client.get(
        "/api/v1/book",
        params={
            "pair": "BTC/EUR",
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": now.isoformat(),
        },
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert body[0]["pair"] == "BTC/EUR"
    assert [row["bid"] for row in body] == [102.0, 101.0, 100.0]  # ascending by ts


async def test_readonly_can_get_orderflow_daily(authed_client: AsyncClient, session: AsyncSession) -> None:
    from datetime import date

    from kaupo.data.orderflow_daily import upsert_orderflow_daily
    from kaupo.domain import OrderflowDaily

    rows = [
        OrderflowDaily(
            exchange="kraken",
            pair="BTC/EUR",
            day=date(2026, 8, 26) + timedelta(days=i),
            trade_count=10 + i,
            buy_count=6,
            sell_count=4,
            buy_volume=3.0,
            sell_volume=2.0,
            max_trade_size=1.5,
            book_snapshots=24,
            spread_mean_bps=5.0,
            spread_max_bps=9.0,
        )
        for i in range(3)
    ]
    await upsert_orderflow_daily(session, rows)
    await session.commit()

    headers = {"Authorization": "Bearer readonly-secret"}
    r = await authed_client.get(
        "/api/v1/orderflow/daily",
        params={"pair": "BTC/EUR", "start": "2026-08-26", "end": "2026-08-29"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert [row["day"] for row in body] == ["2026-08-26", "2026-08-27", "2026-08-28"]  # ascending
    assert body[0]["trade_count"] == 10


async def test_settings_readonly_get_admin_put(authed_client: AsyncClient) -> None:
    readonly = {"Authorization": "Bearer readonly-secret"}
    r = await authed_client.get("/api/v1/settings", headers=readonly)
    assert r.status_code == 200

    r = await authed_client.put("/api/v1/settings", json={"shadow_timeframe": "4h"}, headers=readonly)
    assert r.status_code == 403

    admin = {"Authorization": "Bearer admin-secret"}
    r = await authed_client.put("/api/v1/settings", json={"shadow_timeframe": "4h"}, headers=admin)
    assert r.status_code == 200
    assert r.json()["shadow_timeframe"] == "4h"


async def test_research_can_read_and_submit_backtests(authed_client: AsyncClient) -> None:
    r = await authed_client.get("/api/v1/runs", headers=RESEARCH)
    assert r.status_code == 200
    r = await authed_client.get("/api/v1/assignments", headers=RESEARCH)
    assert r.status_code == 200

    r = await authed_client.post(
        "/api/v1/backtests", json={"strategy": "regime-switch", "pair": "BTC/EUR"}, headers=RESEARCH
    )
    assert r.status_code == 202
    r = await authed_client.get(f"/api/v1/backtests/{r.json()['run_id']}", headers=RESEARCH)
    assert r.status_code == 200


async def test_research_manages_shadow_assignments(authed_client: AsyncClient, session: AsyncSession) -> None:
    body = {"id": "research-shadow", "strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"}
    r = await authed_client.post("/api/v1/assignments", json=body, headers=RESEARCH)
    assert r.status_code == 201
    assert r.json()["mode"] == "shadow"

    r = await authed_client.put(
        "/api/v1/assignments/research-shadow", json={"timeframe": "4h"}, headers=RESEARCH
    )
    assert r.status_code == 200
    assert r.json()["timeframe"] == "4h"

    r = await authed_client.delete("/api/v1/assignments/research-shadow", headers=RESEARCH)
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # re-enabling a disabled shadow row is an update, and allowed
    r = await authed_client.put(
        "/api/v1/assignments/research-shadow", json={"enabled": True}, headers=RESEARCH
    )
    assert r.status_code == 200

    # scope today: any shadow row, including one an admin created
    await _seed_assignment(session, "admin-shadow", "shadow")
    r = await authed_client.put("/api/v1/assignments/admin-shadow", json={"enabled": False}, headers=RESEARCH)
    assert r.status_code == 200


@pytest.mark.parametrize("mode", ["live", "backtest"])
async def test_research_cannot_create_non_shadow_assignment(authed_client: AsyncClient, mode: str) -> None:
    body = {
        "id": "sneaky",
        "strategy_id": "regime-switch",
        "pair": "SOL/EUR",
        "timeframe": "4h",
        "mode": mode,
    }
    r = await authed_client.post("/api/v1/assignments", json=body, headers=RESEARCH)
    assert r.status_code == 403
    rows = (await authed_client.get("/api/v1/assignments", headers=ADMIN)).json()
    assert rows == []


async def test_research_cannot_touch_live_assignment(
    authed_client: AsyncClient, session: AsyncSession
) -> None:
    await _seed_assignment(session, "live-sol-4h", "live")
    before = await _assignment(authed_client, "live-sol-4h")

    for change in ({"enabled": False}, {"params": {"x": 1}}, {"pair": "BTC/EUR"}, {"starting_cash": 1.0}):
        r = await authed_client.put("/api/v1/assignments/live-sol-4h", json=change, headers=RESEARCH)
        assert r.status_code == 403, change
    r = await authed_client.delete("/api/v1/assignments/live-sol-4h", headers=RESEARCH)
    assert r.status_code == 403

    # the id of a live row cannot be reclaimed as a shadow row either
    body = {"id": "live-sol-4h", "strategy_id": "regime-switch", "pair": "SOL/EUR", "timeframe": "4h"}
    r = await authed_client.post("/api/v1/assignments", json=body, headers=RESEARCH)
    assert r.status_code == 409

    assert await _assignment(authed_client, "live-sol-4h") == before


async def test_research_cannot_use_admin_controls_or_settings(authed_client: AsyncClient) -> None:
    for command in ("kill", "pause", "resume", "switch"):
        r = await authed_client.post(f"/api/v1/control/{command}", json={}, headers=RESEARCH)
        assert r.status_code == 403, command
    r = await authed_client.get("/api/v1/settings", headers=RESEARCH)
    assert r.status_code == 200
    r = await authed_client.put("/api/v1/settings", json={"shadow_timeframe": "4h"}, headers=RESEARCH)
    assert r.status_code == 403


async def test_readonly_cannot_write_assignments(authed_client: AsyncClient, session: AsyncSession) -> None:
    await _seed_assignment(session, "some-shadow", "shadow")
    body = {"strategy_id": "regime-switch", "pair": "BTC/EUR", "timeframe": "1h"}
    r = await authed_client.post("/api/v1/assignments", json=body, headers=READONLY)
    assert r.status_code == 403
    r = await authed_client.put("/api/v1/assignments/some-shadow", json={"enabled": False}, headers=READONLY)
    assert r.status_code == 403
    r = await authed_client.delete("/api/v1/assignments/some-shadow", headers=READONLY)
    assert r.status_code == 403
    assert (await _assignment(authed_client, "some-shadow"))["enabled"] is True


async def test_admin_still_manages_live_assignments(
    authed_client: AsyncClient, session: AsyncSession
) -> None:
    await _seed_assignment(session, "live-sol-4h", "live")
    r = await authed_client.put("/api/v1/assignments/live-sol-4h", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200
    r = await authed_client.delete("/api/v1/assignments/live-sol-4h", headers=ADMIN)
    assert r.status_code == 200
    body = {
        "id": "live-2",
        "strategy_id": "regime-switch",
        "pair": "SOL/EUR",
        "timeframe": "4h",
        "mode": "live",
    }
    r = await authed_client.post("/api/v1/assignments", json=body, headers=ADMIN)
    assert r.status_code == 201


async def test_admin_full_access(authed_client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer admin-secret"}
    r = await authed_client.get("/api/v1/runs", headers=headers)
    assert r.status_code == 200
    r = await authed_client.post("/api/v1/control/pause", json={}, headers=headers)
    assert r.status_code == 200


async def _add_command(session: AsyncSession, command: str, run_id: str | None, age_s: int = 0) -> None:
    session.add(
        EventRow(
            id=new_id(),
            ts=utc_now() - timedelta(seconds=age_s),
            level="info",
            source="control",
            message="test",
            data={"command": command, "run_id": run_id},
        )
    )
    await session.commit()


async def test_control_probe(session: AsyncSession) -> None:
    probe = DbControlProbe(get_sessionmaker(), "run-1")
    assert await probe() is None

    await _add_command(session, "pause", "run-1")
    assert await probe() == "pause"

    # resume clears the pause (newer command wins)
    await _add_command(session, "resume", "run-1")
    assert await probe() is None

    # commands for other runs don't affect us
    await _add_command(session, "pause", "run-2")
    assert await probe() is None

    # global (run_id null) applies
    await _add_command(session, "kill", None)
    assert await probe() == "kill"


async def test_control_probe_ignores_commands_older_than_run(session: AsyncSession) -> None:
    await _add_command(session, "kill", None, age_s=3600)  # stale global kill
    probe = DbControlProbe(get_sessionmaker(), "run-new")
    assert await probe() is None  # ignored: issued before the run started

    await _add_command(session, "pause", "run-new")  # fresh
    assert await probe() == "pause"


async def test_control_probe_kill_is_terminal(session: AsyncSession) -> None:
    probe = DbControlProbe(get_sessionmaker(), "run-x")
    await _add_command(session, "kill", "run-x")
    assert await probe() == "kill"
    await _add_command(session, "resume", "run-x")
    assert await probe() == "kill"  # resume cannot un-kill


async def test_control_probe_switch_is_recognized_and_terminal(session: AsyncSession) -> None:
    probe = DbControlProbe(get_sessionmaker(), "run-s")
    assert await probe() is None

    await _add_command(session, "switch", "run-s")
    assert await probe() == "switch"

    await _add_command(session, "resume", "run-s")
    assert await probe() == "switch"  # terminal, like kill


async def test_control_probe_ignores_stale_switch(session: AsyncSession) -> None:
    await _add_command(session, "switch", None, age_s=3600)  # stale global switch
    probe = DbControlProbe(get_sessionmaker(), "run-new")
    assert await probe() is None  # ignored: issued before the run started
