"""BinanceClient.fetch_open_interest_history: raw mapping, validation, malformed rows."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.data.binance import BINANCE_OI_PAGE_SIZE, BinanceClient

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BASE_TS = datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC)


class StubFutures:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.params: dict | None = None
        self.closed = False

    async def fapiDataGetOpenInterestHist(self, params):  # type: ignore[no-untyped-def]  # ccxt implicit name
        self.params = params
        return self.rows

    async def close(self) -> None:
        self.closed = True


class StubSpot:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def entry(ts: datetime | None, oi_base: object, oi_quote: object) -> dict:
    return {
        "symbol": "BTCUSDT",
        "sumOpenInterest": oi_base,
        "sumOpenInterestValue": oi_quote,
        "timestamp": int(ts.timestamp() * 1000) if ts is not None else None,
    }


def make_client(rows: list[dict]) -> tuple[BinanceClient, StubFutures]:
    client = BinanceClient(now=lambda: NOW)
    stub = StubFutures(rows)
    client._futures = stub  # type: ignore[assignment]
    return client, stub


async def test_maps_rows_to_open_interest() -> None:
    rows = [
        entry(BASE_TS, "20403.637", "150570784.07"),
        entry(BASE_TS + timedelta(hours=1), "20410.0", "150600000.5"),
    ]
    client, stub = make_client(rows)

    snapshots = await client.fetch_open_interest_history("BTC", since=BASE_TS)

    assert len(snapshots) == 2
    first = snapshots[0]
    assert (first.exchange, first.base_asset, first.ts) == ("binance", "BTC", BASE_TS)
    assert first.oi_base == 20403.637
    assert first.oi_quote == 150570784.07
    assert stub.params == {
        "symbol": "BTCUSDT",
        "period": "1h",
        "limit": BINANCE_OI_PAGE_SIZE,
        "startTime": int(BASE_TS.timestamp() * 1000),
    }


async def test_base_asset_is_uppercased() -> None:
    client, stub = make_client([entry(BASE_TS, "1.0", "2.0")])
    snapshots = await client.fetch_open_interest_history(" sol ")
    assert stub.params is not None
    assert stub.params["symbol"] == "SOLUSDT"
    assert snapshots[0].base_asset == "SOL"


async def test_malformed_rows_are_dropped() -> None:
    rows = [
        entry(BASE_TS, None, "150570784.07"),  # missing oi_base
        entry(BASE_TS, "20403.637", None),  # missing oi_quote
        entry(BASE_TS, "not-a-number", "150570784.07"),  # unparsable
        entry(BASE_TS, float("nan"), "150570784.07"),  # non-finite
        entry(None, "20403.637", "150570784.07"),  # missing ts
        {"symbol": "BTCUSDT", "sumOpenInterest": "1.0", "sumOpenInterestValue": "2.0", "timestamp": 0},
        entry(BASE_TS, "20403.637", "150570784.07"),  # the one good row
    ]
    client, _ = make_client(rows)
    snapshots = await client.fetch_open_interest_history("BTC")
    assert [s.oi_base for s in snapshots] == [20403.637]


async def test_blank_base_asset_rejected() -> None:
    client, _ = make_client([])
    with pytest.raises(ValueError, match="Invalid base asset"):
        await client.fetch_open_interest_history("  ")


async def test_close_closes_spot_and_futures() -> None:
    client, futures = make_client([])
    spot = StubSpot()
    client._exchange = spot  # type: ignore[assignment]
    await client.close()
    assert spot.closed is True
    assert futures.closed is True
