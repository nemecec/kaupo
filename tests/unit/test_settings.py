"""resolve_shadow_config: CLI flags seed a fresh DB; a stored value always wins."""

from typing import Any

import pytest

import kaupo.data.settings as settings_mod


def _fake_repo(initial: dict[str, str]) -> tuple[dict[str, str], Any]:
    """In-memory get_or_seed with the real insert-if-absent semantics."""
    store = dict(initial)

    async def fake_get_or_seed(session: Any, key: str, default: str) -> str:
        return store.setdefault(key, default)

    return store, fake_get_or_seed


async def test_defaults_fill_and_seed_an_empty_db(monkeypatch: pytest.MonkeyPatch) -> None:
    store, fake = _fake_repo({})
    monkeypatch.setattr(settings_mod, "get_or_seed", fake)

    resolved = await settings_mod.resolve_shadow_config(None)  # type: ignore[arg-type]

    assert resolved.strategy == "regime-switch"
    assert resolved.pair == "BTC/EUR"
    assert resolved.timeframe == "1h"
    assert store == {
        "shadow_strategy": "regime-switch",
        "shadow_pair": "BTC/EUR",
        "shadow_timeframe": "1h",
    }


async def test_cli_flags_seed_a_fresh_db(monkeypatch: pytest.MonkeyPatch) -> None:
    store, fake = _fake_repo({})
    monkeypatch.setattr(settings_mod, "get_or_seed", fake)

    resolved = await settings_mod.resolve_shadow_config(
        None,
        strategy="my-strat",
        pair="ETH/EUR",
        timeframe="4h",  # type: ignore[arg-type]
    )

    assert (resolved.strategy, resolved.pair, resolved.timeframe) == ("my-strat", "ETH/EUR", "4h")
    assert store == {"shadow_strategy": "my-strat", "shadow_pair": "ETH/EUR", "shadow_timeframe": "4h"}


async def test_partial_flags_mix_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    store, fake = _fake_repo({})
    monkeypatch.setattr(settings_mod, "get_or_seed", fake)

    resolved = await settings_mod.resolve_shadow_config(None, strategy="my-strat")  # type: ignore[arg-type]

    assert (resolved.strategy, resolved.pair, resolved.timeframe) == ("my-strat", "BTC/EUR", "1h")
    assert store == {"shadow_strategy": "my-strat", "shadow_pair": "BTC/EUR", "shadow_timeframe": "1h"}


async def test_stored_values_beat_cli_flags_and_are_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # operator changed the strategy via the API; the container still passes
    # the old compose flags — the stored setting must win
    store, fake = _fake_repo(
        {"shadow_strategy": "new-strat", "shadow_pair": "ETH/EUR", "shadow_timeframe": "4h"}
    )
    monkeypatch.setattr(settings_mod, "get_or_seed", fake)

    resolved = await settings_mod.resolve_shadow_config(
        None,
        strategy="old-strat",
        pair="BTC/EUR",
        timeframe="1h",  # type: ignore[arg-type]
    )

    assert (resolved.strategy, resolved.pair, resolved.timeframe) == ("new-strat", "ETH/EUR", "4h")
    assert store["shadow_strategy"] == "new-strat"  # seed did not clobber


async def test_missing_keys_seed_around_stored_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    store, fake = _fake_repo({"shadow_strategy": "new-strat"})
    monkeypatch.setattr(settings_mod, "get_or_seed", fake)

    resolved = await settings_mod.resolve_shadow_config(None, pair="ETH/EUR")  # type: ignore[arg-type]

    assert (resolved.strategy, resolved.pair, resolved.timeframe) == ("new-strat", "ETH/EUR", "1h")
    assert store == {"shadow_strategy": "new-strat", "shadow_pair": "ETH/EUR", "shadow_timeframe": "1h"}
