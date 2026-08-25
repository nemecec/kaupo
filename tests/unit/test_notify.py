"""ntfy alerting: send_alert behavior with and without a configured topic."""

from typing import Any

import pytest

import kaupo.core.notify as notify


def _fake_settings(topic: str) -> Any:
    class S:
        notify_ntfy_topic = topic

    return S()


async def test_send_alert_no_topic_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "get_settings", lambda: _fake_settings(""))

    async def fake_post(topic: str, message: str) -> None:
        calls.append((topic, message))

    monkeypatch.setattr(notify, "_post", fake_post)
    await notify.send_alert("hello")
    assert calls == []


async def test_send_alert_posts_with_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "get_settings", lambda: _fake_settings("kaupo-test"))

    async def fake_post(topic: str, message: str) -> None:
        calls.append((topic, message))

    monkeypatch.setattr(notify, "_post", fake_post)
    await notify.send_alert("hello")
    assert calls == [("kaupo-test", "hello")]


async def test_send_alert_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "get_settings", lambda: _fake_settings("kaupo-test"))

    async def boom(topic: str, message: str) -> None:
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(notify, "_post", boom)
    await notify.send_alert("hello")  # must not raise
