"""Settings repository and the /api/v1/settings routes against real Postgres."""

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import get_settings
from kaupo.data import settings as settings_repo
from kaupo.db.models import EventRow, RunRow, SettingRow
from kaupo.db.session import dispose_engine
from kaupo.domain import utc_now

pytestmark = pytest.mark.integration


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


def _add_run(session: AsyncSession, run_id: str, mode: str, status: str) -> None:
    session.add(
        RunRow(
            id=run_id,
            mode=mode,
            strategy_id="s",
            strategy_version="v",
            started_at=utc_now(),
            status=status,
            config={},
        )
    )


async def _control_events(session: AsyncSession) -> list[EventRow]:
    rows = await session.execute(select(EventRow).where(EventRow.source == "control"))
    return list(rows.scalars().all())


# --- repository -----------------------------------------------------------


async def test_get_settings_empty(session: AsyncSession) -> None:
    assert await settings_repo.get_settings(session) == {}


async def test_upsert_inserts_then_overwrites(session: AsyncSession) -> None:
    await settings_repo.upsert_settings(session, {"shadow_strategy": "a", "shadow_pair": "BTC/EUR"})
    await session.commit()
    assert await settings_repo.get_settings(session) == {
        "shadow_strategy": "a",
        "shadow_pair": "BTC/EUR",
    }

    first = (
        await session.execute(select(SettingRow).where(SettingRow.key == "shadow_strategy"))
    ).scalar_one()

    await settings_repo.upsert_settings(session, {"shadow_strategy": "b"})
    await session.commit()
    stored = await settings_repo.get_settings(session)
    assert stored["shadow_strategy"] == "b"
    assert stored["shadow_pair"] == "BTC/EUR"  # untouched key survives

    second = (
        await session.execute(select(SettingRow).where(SettingRow.key == "shadow_strategy"))
    ).scalar_one()
    assert second.updated_at >= first.updated_at


async def test_upsert_empty_dict_is_noop(session: AsyncSession) -> None:
    await settings_repo.upsert_settings(session, {})
    await session.commit()
    assert await settings_repo.get_settings(session) == {}


async def test_get_or_seed_inserts_default_only_when_absent(session: AsyncSession) -> None:
    assert await settings_repo.get_or_seed(session, "shadow_timeframe", "1h") == "1h"
    await session.commit()
    assert (await settings_repo.get_settings(session))["shadow_timeframe"] == "1h"

    # an operator/API change must survive later seeding attempts
    await settings_repo.upsert_settings(session, {"shadow_timeframe": "4h"})
    await session.commit()
    assert await settings_repo.get_or_seed(session, "shadow_timeframe", "1h") == "4h"
    assert (await settings_repo.get_settings(session))["shadow_timeframe"] == "4h"


# --- GET /api/v1/settings ---------------------------------------------------


async def test_get_returns_defaults_when_nothing_stored(client: AsyncClient) -> None:
    r = await client.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["shadow_strategy"] == "regime-switch"
    assert body["shadow_pair"] == "BTC/EUR"
    assert body["shadow_timeframe"] == "1h"
    assert body["updated_at"] == {}


async def test_get_fills_defaults_around_stored_keys(client: AsyncClient, session: AsyncSession) -> None:
    await settings_repo.upsert_settings(session, {"shadow_timeframe": "4h"})
    await session.commit()

    r = await client.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["shadow_timeframe"] == "4h"
    assert body["shadow_strategy"] == "regime-switch"  # default filled in
    assert set(body["updated_at"]) == {"shadow_timeframe"}  # stored keys only


# --- PUT /api/v1/settings ---------------------------------------------------


async def test_put_upserts_and_normalizes(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.put(
        "/api/v1/settings",
        json={"shadow_strategy": "regime-switch", "shadow_pair": "eth/eur", "shadow_timeframe": "4h"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["shadow_pair"] == "ETH/EUR"  # normalized
    assert body["shadow_timeframe"] == "4h"
    assert set(body["updated_at"]) == {"shadow_strategy", "shadow_pair", "shadow_timeframe"}

    stored = await settings_repo.get_settings(session)
    assert stored == {
        "shadow_strategy": "regime-switch",
        "shadow_pair": "ETH/EUR",
        "shadow_timeframe": "4h",
    }


async def test_put_change_writes_switch_event_for_running_shadow_runs_only(
    client: AsyncClient, session: AsyncSession
) -> None:
    _add_run(session, "shadow-running", "shadow", "running")
    _add_run(session, "shadow-halted", "shadow", "halted")
    _add_run(session, "backtest-running", "backtest", "running")
    await session.commit()

    r = await client.put("/api/v1/settings", json={"shadow_strategy": "regime-switch"})
    assert r.status_code == 200

    events = await _control_events(session)
    assert len(events) == 1
    assert events[0].data == {
        "command": "switch",
        "run_id": "shadow-running",
        "settings": {"shadow_strategy": "regime-switch"},
    }


async def test_put_without_changes_writes_no_event(client: AsyncClient, session: AsyncSession) -> None:
    await settings_repo.upsert_settings(session, {"shadow_strategy": "regime-switch"})
    _add_run(session, "shadow-running", "shadow", "running")
    await session.commit()

    r = await client.put("/api/v1/settings", json={"shadow_strategy": "regime-switch"})
    assert r.status_code == 200
    assert await _control_events(session) == []


async def test_put_with_nothing_running_upserts_but_writes_no_event(
    client: AsyncClient, session: AsyncSession
) -> None:
    r = await client.put("/api/v1/settings", json={"shadow_timeframe": "4h"})
    assert r.status_code == 200
    assert await _control_events(session) == []
    assert (await settings_repo.get_settings(session))["shadow_timeframe"] == "4h"


async def test_put_rejects_empty_body(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.put("/api/v1/settings", json={})
    assert r.status_code == 422
    assert await settings_repo.get_settings(session) == {}


async def test_put_rejects_unknown_strategy(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.put("/api/v1/settings", json={"shadow_strategy": "does-not-exist"})
    assert r.status_code == 422
    assert "unknown strategy" in r.json()["detail"]
    assert await settings_repo.get_settings(session) == {}


async def test_put_rejects_bad_pair(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.put("/api/v1/settings", json={"shadow_pair": "BTCEUR"})
    assert r.status_code == 422
    assert await settings_repo.get_settings(session) == {}


async def test_put_rejects_bad_timeframe(client: AsyncClient, session: AsyncSession) -> None:
    r = await client.put("/api/v1/settings", json={"shadow_timeframe": "2h"})
    assert r.status_code == 422
    assert await settings_repo.get_settings(session) == {}
