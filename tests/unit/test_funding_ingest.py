"""backfill_funding: forward paging, per-batch upsert, no-progress guard."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kaupo.data.ingest import backfill_funding
from kaupo.domain import FundingRate

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def rate(i: int, hours: int = 8) -> FundingRate:
    return FundingRate(
        exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=i * hours), rate=0.0001
    )


class FakeFundingClient:
    """Stands in for BinanceClient: serves canned funding rates from ``pages``."""

    def __init__(self, pages: list[list[FundingRate]]) -> None:
        self.pages = pages
        self.calls: list[datetime | None] = []

    async def fetch_funding_rates(
        self, base_asset: str, since: datetime | None = None, limit: int = 1000
    ) -> list[FundingRate]:
        self.calls.append(since)
        if not self.pages:
            return []
        return self.pages.pop(0)


class FakeSession:
    def __init__(self, store: list[FundingRate]) -> None:
        self.store = store

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        pass


def fake_sessionmaker(store: list[FundingRate]) -> Any:
    def make() -> FakeSession:
        return FakeSession(store)

    return make


@pytest.fixture
def capture_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    import kaupo.data.ingest as ingest

    async def fake_upsert(session: Any, rates: list[FundingRate]) -> int:
        session.store.extend(rates)
        return len(rates)

    monkeypatch.setattr(ingest, "upsert_funding_rates", fake_upsert)


@pytest.mark.usefixtures("capture_upserts")
class TestBackfillFunding:
    async def test_paginates_until_end(self) -> None:
        store: list[FundingRate] = []
        pages = [[rate(0), rate(1), rate(2)], [rate(3), rate(4)]]
        client = FakeFundingClient(pages)

        end = BASE + timedelta(hours=5 * 8)
        total = await backfill_funding(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            "BTC",
            start=BASE,
            end=end,
        )
        assert total == 5
        assert [r.ts for r in store] == [BASE + timedelta(hours=i * 8) for i in range(5)]
        # second page started a millisecond after the last point of the first page
        assert client.calls[1] == BASE + timedelta(hours=16, milliseconds=1)

    async def test_stops_on_empty_page(self) -> None:
        store: list[FundingRate] = []
        client = FakeFundingClient([[], []])
        total = await backfill_funding(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            "BTC",
            start=BASE,
            end=BASE + timedelta(days=10),
        )
        assert total == 0

    async def test_no_progress_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kaupo.data.ingest as ingest

        async def fake_upsert(session: Any, rates: list[FundingRate]) -> int:
            return len(rates)

        monkeypatch.setattr(ingest, "upsert_funding_rates", fake_upsert)

        # client keeps returning a point at the same ts -> no progress
        store: list[FundingRate] = []
        client = FakeFundingClient([[rate(0)], [rate(0)]])
        total = await backfill_funding(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            "BTC",
            start=BASE,
            end=BASE + timedelta(days=10),
        )
        assert total == 1  # stopped after detecting no progress

    async def test_points_at_or_after_end_are_dropped(self) -> None:
        store: list[FundingRate] = []
        end = BASE + timedelta(hours=16)
        # the page holds a point exactly at `end` ([start, end) semantics)
        client = FakeFundingClient([[rate(0), rate(1), rate(2)]])
        total = await backfill_funding(
            client,  # type: ignore[arg-type]
            fake_sessionmaker(store),
            "BTC",
            start=BASE,
            end=end,
        )
        assert total == 2
        assert [r.ts for r in store] == [BASE, BASE + timedelta(hours=8)]
