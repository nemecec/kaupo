"""backfill_trades: forward paging, cursor threading, empty-window skips."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kaupo.data.ingest import backfill_trades
from kaupo.domain import Pair, TradeTick

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def tick(i: int, minutes: int = 1) -> TradeTick:
    return TradeTick(
        exchange="kraken",
        pair=str(PAIR),
        ts=BASE + timedelta(minutes=i * minutes),
        price=100.0 + i,
        size=0.1,
        side="buy",
    )


class FakeTradeClient:
    """Stands in for a venue client: serves canned ticks from ``pages``."""

    def __init__(
        self,
        pages: list[list[TradeTick]],
        cursors: list[str | None] | None = None,
        window: timedelta | None = None,
    ) -> None:
        self.pages = pages
        self.cursors = cursors or []
        self.trades_page_window = window
        self.calls: list[tuple[datetime | None, str | None]] = []  # (since, cursor) per call
        self._trades_cursor: str | None = None

    @property
    def trades_cursor(self) -> str | None:
        return self._trades_cursor

    async def fetch_trades(
        self,
        pair: Pair,
        since: datetime | None = None,
        limit: int | None = None,
        *,
        cursor: str | None = None,
    ) -> list[TradeTick]:
        self.calls.append((since, cursor))
        if self.cursors:
            self._trades_cursor = self.cursors.pop(0)
        if not self.pages:
            return []
        return self.pages.pop(0)


class FakeSession:
    def __init__(self, store: list[TradeTick]) -> None:
        self.store = store

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        pass


def fake_sessionmaker(store: list[TradeTick]) -> Any:
    def make() -> FakeSession:
        return FakeSession(store)

    return make


@pytest.fixture
def capture_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.ingest as ingest

    async def fake_upsert(session: Any, ticks: list[TradeTick]) -> int:
        session.store.extend(ticks)
        return len(ticks)

    monkeypatch.setattr(ingest, "upsert_trade_ticks", fake_upsert)


@pytest.mark.usefixtures("capture_upserts")
class TestBackfillTrades:
    async def test_paginates_until_end(self) -> None:
        store: list[TradeTick] = []
        pages = [[tick(0), tick(1), tick(2)], [tick(3), tick(4)]]
        client = FakeTradeClient(pages)

        end = BASE + timedelta(minutes=5)
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=end,
        )
        assert total == 5
        assert [t.ts for t in store] == [BASE + timedelta(minutes=i) for i in range(5)]
        # second page started a millisecond after the last tick of the first page
        assert client.calls[1] == (BASE + timedelta(minutes=2, milliseconds=1), None)

    async def test_response_cursor_is_threaded_between_pages(self) -> None:
        store: list[TradeTick] = []
        client = FakeTradeClient([[tick(0), tick(1)], [tick(2)]], cursors=["ns-cursor-1", "ns-cursor-2"])

        end = BASE + timedelta(minutes=3)
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=end,
        )
        assert total == 3
        # the first call has no cursor; later calls reuse the response cursor
        assert client.calls[0] == (BASE, None)
        assert client.calls[1] == (BASE + timedelta(minutes=1, milliseconds=1), "ns-cursor-1")

    async def test_stops_on_empty_page(self) -> None:
        store: list[TradeTick] = []
        client = FakeTradeClient([[], []])
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(days=1),
        )
        assert total == 0
        assert len(client.calls) == 1

    async def test_empty_window_is_skipped_not_stopped(self) -> None:
        """Binance aggTrades: an empty page is an empty hour, not the live edge."""
        store: list[TradeTick] = []
        client = FakeTradeClient(
            [[], [tick(0, minutes=90)]],  # a quiet hour, then one trade 90 min in
            window=timedelta(hours=1),
        )
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(hours=3),
        )
        assert total == 1
        # after the empty page the window was skipped a full hour forward
        assert client.calls[1] == (BASE + timedelta(hours=1), None)

    async def test_empty_window_at_the_range_end_stops(self) -> None:
        store: list[TradeTick] = []
        client = FakeTradeClient([[]], window=timedelta(hours=1))
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(minutes=30),  # less than one window ahead: the live edge
        )
        assert total == 0
        assert len(client.calls) == 1

    async def test_stops_when_a_page_has_only_duplicates(self) -> None:
        """Kraken's cursor is inclusive: the live edge ends with an all-dup page."""
        store: list[TradeTick] = []
        client = FakeTradeClient([[tick(0)], [tick(0)]])  # the boundary trade re-served
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(days=1),
        )
        assert total == 1  # upserted once, the duplicate page ended the run quietly
        assert [t.ts for t in store] == [BASE]

    async def test_same_ms_continuation_pages_with_cursor(self) -> None:
        """A burst of same-ms trades pages by the cursor, not the ts."""
        ts = BASE
        t_a = TradeTick(exchange="kraken", pair=str(PAIR), ts=ts, price=100.0, size=0.1, side="buy")
        t_b = TradeTick(exchange="kraken", pair=str(PAIR), ts=ts, price=101.0, size=0.1, side="buy")
        t_c = TradeTick(exchange="kraken", pair=str(PAIR), ts=ts, price=102.0, size=0.1, side="buy")
        store: list[TradeTick] = []
        client = FakeTradeClient([[t_a, t_b], [t_b, t_c]], cursors=["ns-1", "ns-2"])  # t_b re-served

        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(minutes=1),
        )
        assert total == 3
        assert store == [t_a, t_b, t_c]  # the re-served t_b was skipped, not stopped on

    async def test_no_progress_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kaupo.data.ingest as ingest

        async def fake_upsert(session: Any, ticks: list[TradeTick]) -> int:
            return len(ticks)

        monkeypatch.setattr(ingest, "upsert_trade_ticks", fake_upsert)

        # the second page goes backwards without a cursor -> no progress
        store: list[TradeTick] = []
        client = FakeTradeClient([[tick(1)], [tick(0)]])
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=BASE + timedelta(days=1),
        )
        assert total == 1  # stopped after detecting no progress

    async def test_ticks_at_or_after_end_are_dropped(self) -> None:
        store: list[TradeTick] = []
        end = BASE + timedelta(minutes=2)
        # the page holds a tick exactly at `end` ([start, end) semantics)
        client = FakeTradeClient([[tick(0), tick(1), tick(2)]])
        total = await backfill_trades(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            PAIR,
            start=BASE,
            end=end,
        )
        assert total == 2
        assert [t.ts for t in store] == [BASE, BASE + timedelta(minutes=1)]
