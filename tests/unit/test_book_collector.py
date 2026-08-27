"""Book-collector cycle: per-pair failures skip, upsert/prune wiring, stop event."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import kaupo.core.book_collector as collector_mod
from kaupo.config import Settings
from kaupo.core.book_collector import collect_cycle, run_book_collector
from kaupo.domain import BookSnapshot, Pair

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
PAIRS = [Pair.parse("BTC/EUR"), Pair.parse("ETH/EUR"), Pair.parse("SOL/EUR")]


def snapshot(pair: str, ts: datetime = NOW) -> BookSnapshot:
    return BookSnapshot(exchange="kraken", pair=pair, ts=ts, bid=100.0, ask=100.5, bid_size=1.0, ask_size=2.0)


class FakeClient:
    """Serves canned fetch_book_top results; an exception value is raised."""

    exchange_id = "kraken"

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def fetch_book_top(self, pair: Pair) -> BookSnapshot | None:
        self.calls.append(str(pair))
        result = self.results[str(pair)]
        if isinstance(result, Exception):
            raise result
        return result


class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


def fake_sessionmaker() -> Any:
    return lambda: _FakeSession()


@pytest.fixture
def repo_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the repo functions with recorders (the cycle tests need no DB)."""
    store: dict[str, Any] = {"upserted": [], "pruned": []}

    async def fake_upsert(session: Any, snapshots: list[BookSnapshot]) -> int:
        store["upserted"].extend(snapshots)
        return len(snapshots)

    async def fake_prune(session: Any, exchange: str, pair: str, older_than: datetime) -> int:
        store["pruned"].append((exchange, pair, older_than))
        return 1

    monkeypatch.setattr(collector_mod, "upsert_book_snapshots", fake_upsert)
    monkeypatch.setattr(collector_mod, "prune_book_snapshots", fake_prune)
    return store


async def test_cycle_stores_good_pairs_and_skips_failures(repo_spy: dict[str, Any]) -> None:
    client = FakeClient(
        {
            "BTC/EUR": snapshot("BTC/EUR"),
            "ETH/EUR": ConnectionError("boom"),  # venue hiccup: logged and skipped
            "SOL/EUR": None,  # no usable bid/ask: dropped by the client
        }
    )

    stored, pruned = await collect_cycle(client, fake_sessionmaker(), PAIRS, 30, now=NOW)

    assert client.calls == ["BTC/EUR", "ETH/EUR", "SOL/EUR"]  # a failure does not end the cycle
    assert stored == 1
    assert [s.pair for s in repo_spy["upserted"]] == ["BTC/EUR"]
    # every pair is pruned after the cycle, exchange from the client
    assert [(e, p) for e, p, _ in repo_spy["pruned"]] == [
        ("kraken", "BTC/EUR"),
        ("kraken", "ETH/EUR"),
        ("kraken", "SOL/EUR"),
    ]
    assert pruned == 3


async def test_cycle_prunes_at_the_retention_cutoff(repo_spy: dict[str, Any]) -> None:
    client = FakeClient({"BTC/EUR": snapshot("BTC/EUR"), "ETH/EUR": None, "SOL/EUR": None})

    await collect_cycle(client, fake_sessionmaker(), PAIRS, 30, now=NOW)

    assert repo_spy["pruned"][0][2] == NOW - timedelta(days=30)


async def test_cycle_without_snapshots_still_prunes(repo_spy: dict[str, Any]) -> None:
    client = FakeClient({str(p): None for p in PAIRS})

    stored, pruned = await collect_cycle(client, fake_sessionmaker(), PAIRS, 30, now=NOW)

    assert stored == 0
    assert repo_spy["upserted"] == []
    assert pruned == 3


async def test_loop_runs_cycles_until_stopped(repo_spy: dict[str, Any]) -> None:
    client = FakeClient({str(p): snapshot(str(p)) for p in PAIRS})
    settings = Settings(book_poll_seconds=0.01, book_retention_days=30)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_book_collector(fake_sessionmaker(), settings, stop, client=client, pairs=PAIRS)
    )
    await asyncio.sleep(0.05)  # a few cycles at 10ms poll
    stop.set()
    await task

    assert len(client.calls) >= len(PAIRS)  # at least one full cycle ran
    assert len(repo_spy["upserted"]) == len(client.calls)


async def test_loop_with_stop_already_set_does_no_cycle(repo_spy: dict[str, Any]) -> None:
    client = FakeClient({str(p): snapshot(str(p)) for p in PAIRS})
    settings = Settings(book_poll_seconds=0.01, book_retention_days=30)
    stop = asyncio.Event()
    stop.set()

    await run_book_collector(fake_sessionmaker(), settings, stop, client=client, pairs=PAIRS)

    assert client.calls == []
    assert repo_spy["upserted"] == []
