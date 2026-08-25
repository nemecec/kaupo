"""BinanceClient.fetch_funding_rates: ccxt mapping, validation, malformed rows."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.data.binance import BINANCE_FUNDING_PAGE_SIZE, BinanceClient

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BASE_TS = datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC)


class StubFutures:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.symbol: str | None = None
        self.since: int | None = None
        self.limit: int | None = None
        self.closed = False

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):  # type: ignore[no-untyped-def]
        self.symbol = symbol
        self.since = since
        self.limit = limit
        return self.rows

    async def close(self) -> None:
        self.closed = True


class StubSpot:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def entry(ts: datetime, rate: float | None) -> dict:
    return {
        "info": {},
        "symbol": "BTC/USDT:USDT",
        "fundingRate": rate,
        "timestamp": int(ts.timestamp() * 1000) if ts is not None else None,
        "datetime": ts.isoformat() if ts is not None else None,
    }


def make_client(rows: list[dict]) -> tuple[BinanceClient, StubFutures]:
    client = BinanceClient(now=lambda: NOW)
    stub = StubFutures(rows)
    client._futures = stub  # type: ignore[assignment]
    return client, stub


async def test_maps_rows_to_funding_rates() -> None:
    rows = [entry(BASE_TS, 0.0001), entry(BASE_TS + timedelta(hours=8), -0.0002)]
    client, stub = make_client(rows)

    rates = await client.fetch_funding_rates("BTC", since=BASE_TS)

    assert len(rates) == 2
    first, second = rates
    assert (first.exchange, first.base_asset, first.ts, first.rate) == (
        "binance",
        "BTC",
        BASE_TS,
        0.0001,
    )
    assert second.rate == -0.0002  # negative funding is valid and kept
    assert stub.symbol == "BTC/USDT:USDT"
    assert stub.since == int(BASE_TS.timestamp() * 1000)
    assert stub.limit == BINANCE_FUNDING_PAGE_SIZE


async def test_base_asset_is_uppercased() -> None:
    client, stub = make_client([entry(BASE_TS, 0.0001)])
    rates = await client.fetch_funding_rates(" sol ")
    assert stub.symbol == "SOL/USDT:USDT"
    assert rates[0].base_asset == "SOL"


async def test_malformed_rows_are_dropped() -> None:
    rows = [
        entry(BASE_TS, None),  # missing rate
        entry(BASE_TS, float("nan")),  # non-finite rate
        {"info": {}, "symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "timestamp": None},
        {"info": {}, "symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "timestamp": 0},
        entry(BASE_TS, 0.0003),  # the one good row
    ]
    client, _ = make_client(rows)
    rates = await client.fetch_funding_rates("BTC")
    assert [r.rate for r in rates] == [0.0003]


async def test_blank_base_asset_rejected() -> None:
    client, _ = make_client([])
    with pytest.raises(ValueError, match="Invalid base asset"):
        await client.fetch_funding_rates("  ")


async def test_close_closes_spot_and_futures() -> None:
    client, futures = make_client([])
    spot = StubSpot()
    client._exchange = spot  # type: ignore[assignment]
    await client.close()
    assert spot.closed is True
    assert futures.closed is True
