"""record_halt writes an audit event and pushes an alert (transport mocked)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import kaupo.core.notify as notify
from kaupo.db.models import EventRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import RunId

pytestmark = pytest.mark.integration


async def test_record_halt_writes_event_and_alerts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_post(topic: str, message: str) -> None:
        calls.append((topic, message))

    monkeypatch.setattr(notify, "_post", fake_post)
    monkeypatch.setattr(notify, "get_settings", lambda: type("S", (), {"notify_ntfy_topic": "t"})())

    await notify.record_halt(get_sessionmaker(), RunId("run-1"), "sma-cross", "daily loss limit")

    rows = (await session.execute(select(EventRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].level == "warning"
    assert rows[0].source == "engine"
    assert "daily loss limit" in rows[0].message
    assert rows[0].data["run_id"] == "run-1"
    assert calls == [("t", "Shadow run halted (sma-cross): daily loss limit")]
