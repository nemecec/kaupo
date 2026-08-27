"""fetch_book_top on the venue clients: ccxt ticker mapping, unusable rows."""

from datetime import UTC, datetime
from typing import Any

from kaupo.data.binance import BinanceClient
from kaupo.data.kraken import KrakenClient
from kaupo.domain import Pair

PAIR = Pair.parse("BTC/EUR")
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)


class StubExchange:
    """Stands in for the ccxt exchange: serves a canned ticker."""

    def __init__(self, canned: dict[str, Any]) -> None:
        self.canned = canned
        self.symbols: list[str] = []

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self.symbols.append(symbol)
        return self.canned

    async def close(self) -> None:
        pass


def ticker(
    ts: datetime | None,
    bid: Any,
    ask: Any,
    bid_volume: Any = 1.5,
    ask_volume: Any = 2.5,
) -> dict[str, Any]:
    """A ccxt-normalized ticker structure."""
    return {
        "timestamp": int(ts.timestamp() * 1000) if ts is not None else None,
        "bid": bid,
        "ask": ask,
        "bidVolume": bid_volume,
        "askVolume": ask_volume,
    }


def make_kraken(canned: dict[str, Any]) -> tuple[KrakenClient, StubExchange]:
    client = KrakenClient(now=lambda: NOW)
    stub = StubExchange(canned)
    client._exchange = stub  # type: ignore[assignment]
    return client, stub


def make_binance(canned: dict[str, Any]) -> tuple[BinanceClient, StubExchange]:
    client = BinanceClient(now=lambda: NOW)
    stub = StubExchange(canned)
    client._exchange = stub  # type: ignore[assignment]
    return client, stub


class TestFetchBookTop:
    async def test_ticker_maps_to_a_snapshot(self) -> None:
        client, stub = make_kraken(ticker(BASE, 100.0, 100.5, bid_volume=1.5, ask_volume=2.5))

        snapshot = await client.fetch_book_top(PAIR)

        assert stub.symbols == ["BTC/EUR"]
        assert snapshot is not None
        assert (snapshot.exchange, snapshot.pair, snapshot.ts) == ("kraken", "BTC/EUR", BASE)
        assert (snapshot.bid, snapshot.ask, snapshot.bid_size, snapshot.ask_size) == (
            100.0,
            100.5,
            1.5,
            2.5,
        )

    async def test_binance_client_supports_the_same_call(self) -> None:
        client, _ = make_binance(ticker(BASE, 100.0, 100.5))

        snapshot = await client.fetch_book_top(PAIR)

        assert snapshot is not None
        assert snapshot.exchange == "binance"
        assert (snapshot.bid, snapshot.ask) == (100.0, 100.5)

    async def test_unusable_bid_or_ask_is_dropped(self) -> None:
        for bad in (
            ticker(BASE, None, 100.5),  # missing bid
            ticker(BASE, 100.0, None),  # missing ask
            ticker(BASE, 0.0, 100.5),  # zero bid
            ticker(BASE, 100.0, 0.0),  # zero ask
            ticker(BASE, -1.0, 100.5),  # negative bid
            ticker(BASE, float("nan"), 100.5),  # non-finite bid
        ):
            client, _ = make_kraken(bad)
            assert await client.fetch_book_top(PAIR) is None

    async def test_missing_timestamp_falls_back_to_the_poll_time(self) -> None:
        client, _ = make_kraken(ticker(None, 100.0, 100.5))

        snapshot = await client.fetch_book_top(PAIR)

        assert snapshot is not None
        assert snapshot.ts == NOW

    async def test_missing_sizes_mean_unknown_depth_stored_as_zero(self) -> None:
        client, _ = make_kraken(ticker(BASE, 100.0, 100.5, bid_volume=None, ask_volume=None))

        snapshot = await client.fetch_book_top(PAIR)

        assert snapshot is not None
        assert (snapshot.bid_size, snapshot.ask_size) == (0.0, 0.0)
