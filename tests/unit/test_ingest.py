from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kaupo.data.ingest import LiveCandlePoller, backfill
from kaupo.domain import Candle, Pair, Timeframe

PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int) -> Candle:
    ts = BASE + timedelta(hours=i)
    return Candle(pair=PAIR, timeframe=TF, ts=ts, open=100, high=101, low=99, close=100.5, volume=1.0)


class FakeClient:
    """Stands in for KrakenClient: serves canned candles from ``pages``."""

    def __init__(self, pages: list[list[Candle]]) -> None:
        self.pages = pages
        self.calls: list[datetime | None] = []

    async def fetch_candles(
        self, pair: Pair, timeframe: Timeframe, since: datetime | None = None, limit: int = 720
    ) -> list[Candle]:
        self.calls.append(since)
        if not self.pages:
            return []
        return self.pages.pop(0)


class FakeSession:
    def __init__(self, store: list[Candle]) -> None:
        self.store = store

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        pass

    async def execute(self, stmt: Any) -> None:
        pass


def fake_sessionmaker(store: list[Candle]) -> Any:
    def make() -> FakeSession:
        return FakeSession(store)

    return make


class TestBackfill:
    async def test_paginates_until_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store: list[Candle] = []
        pages = [[candle(0), candle(1), candle(2)], [candle(3), candle(4)]]
        client = FakeClient(pages)

        # capture upserts without a DB
        import kaupo.data.ingest as ingest

        async def fake_upsert(session: Any, candles: list[Candle]) -> int:
            session.store.extend(candles)
            return len(candles)

        monkeypatch.setattr(ingest, "upsert_candles", fake_upsert)

        end = BASE + timedelta(hours=5)
        total = await backfill(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            TF,
            start=BASE,
            end=end,
        )
        assert total == 5
        assert [c.ts for c in store] == [BASE + timedelta(hours=i) for i in range(5)]
        # second page started after the last candle of the first page
        assert client.calls[1] == BASE + timedelta(hours=3)

    async def test_stops_on_empty_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kaupo.data.ingest as ingest

        async def fake_upsert(session: Any, candles: list[Candle]) -> int:
            session.store.extend(candles)
            return len(candles)

        monkeypatch.setattr(ingest, "upsert_candles", fake_upsert)

        store: list[Candle] = []
        client = FakeClient([[], []])
        total = await backfill(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            TF,
            start=BASE,
            end=BASE + timedelta(hours=10),
        )
        assert total == 0

    async def test_no_progress_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kaupo.data.ingest as ingest

        async def fake_upsert(session: Any, candles: list[Candle]) -> int:
            return len(candles)

        monkeypatch.setattr(ingest, "upsert_candles", fake_upsert)

        # client keeps returning a candle at the same ts -> no progress
        store: list[Candle] = []
        client = FakeClient([[candle(0)], [candle(0)]])
        total = await backfill(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            TF,
            start=BASE,
            end=BASE + timedelta(hours=10),
        )
        assert total == 1  # stopped after detecting no progress


class TestLiveCandlePoller:
    async def test_first_poll_returns_only_latest(self) -> None:
        client = FakeClient([[candle(0), candle(1), candle(2)]])
        poller = LiveCandlePoller(client, PAIR, TF, poll_interval_seconds=0)  # type: ignore[arg-type]
        first = await poller.poll_once()
        assert [c.ts for c in first] == [BASE + timedelta(hours=2)]

    async def test_subsequent_polls_return_new_only(self) -> None:
        client = FakeClient(
            [
                [candle(0), candle(1)],
                [candle(1), candle(2)],
                [candle(2)],  # nothing new
                [candle(2), candle(3), candle(4)],
            ]
        )
        poller = LiveCandlePoller(client, PAIR, TF, poll_interval_seconds=0)  # type: ignore[arg-type]

        assert [c.ts for c in await poller.poll_once()] == [BASE + timedelta(hours=1)]
        assert [c.ts for c in await poller.poll_once()] == [BASE + timedelta(hours=2)]
        assert await poller.poll_once() == []
        assert [c.ts for c in await poller.poll_once()] == [
            BASE + timedelta(hours=3),
            BASE + timedelta(hours=4),
        ]

    async def test_empty_exchange_response(self) -> None:
        client = FakeClient([[]])
        poller = LiveCandlePoller(client, PAIR, TF, poll_interval_seconds=0)  # type: ignore[arg-type]
        assert await poller.poll_once() == []


class TestPollerGapRefill:
    async def test_gap_after_outage_is_backfilled(self) -> None:
        # baseline at candle 0; window moved far ahead -> poller must fetch
        # from the baseline, not jump to the newest candle
        all_candles = [candle(i) for i in range(20)]
        client = FakeClient([all_candles[0:1], all_candles])
        poller = LiveCandlePoller(client, PAIR, TF, poll_interval_seconds=0)  # type: ignore[arg-type]

        first = await poller.poll_once()
        assert first == [all_candles[0]]  # baseline established

        # second call: poller detects baseline << window and requests from baseline
        new = await poller.poll_once()
        assert new == all_candles[1:]  # entire gap returned, nothing skipped
