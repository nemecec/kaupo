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
    get_settings.cache_clear()
    await dispose_engine()
    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    os.environ.pop("KAUPO_ADMIN_TOKEN", None)
    os.environ.pop("KAUPO_READONLY_TOKEN", None)
    get_settings.cache_clear()
    await dispose_engine()


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
