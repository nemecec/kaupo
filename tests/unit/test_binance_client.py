"""BinanceClient construction and exchange tagging with a stubbed ccxt exchange."""

from datetime import UTC, datetime, timedelta

from kaupo.data.binance import BINANCE_PAGE_SIZE, BinanceClient
from kaupo.data.kraken import KRAKEN_PAGE_SIZE, KrakenClient
from kaupo.domain import Pair, Timeframe

PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1
NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=UTC)


class StubExchange:
    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows
        self.limit: int | None = None

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):  # type: ignore[no-untyped-def]
        self.limit = limit
        return self.rows

    async def close(self) -> None:
        pass


def ohlcv(ts: datetime, price: float = 100.0) -> list[float]:
    return [int(ts.timestamp() * 1000), price, price + 1, price - 1, price, 1.0]


def test_client_defaults() -> None:
    assert BinanceClient().exchange_id == "binance"
    assert BinanceClient().page_size == BINANCE_PAGE_SIZE
    assert KrakenClient().exchange_id == "kraken"
    assert KrakenClient().page_size == KRAKEN_PAGE_SIZE


async def test_binance_candles_tagged_with_exchange() -> None:
    client = BinanceClient(now=lambda: NOW)
    client._exchange = StubExchange([ohlcv(NOW - timedelta(hours=2))])  # type: ignore[assignment]
    candles = await client.fetch_candles(PAIR, TF)
    assert len(candles) == 1
    assert candles[0].exchange == "binance"


async def test_kraken_candles_tagged_kraken() -> None:
    client = KrakenClient(now=lambda: NOW)
    client._exchange = StubExchange([ohlcv(NOW - timedelta(hours=2))])  # type: ignore[assignment]
    candles = await client.fetch_candles(PAIR, TF)
    assert len(candles) == 1
    assert candles[0].exchange == "kraken"


async def test_default_limit_is_page_size() -> None:
    stub = StubExchange([])
    client = BinanceClient(now=lambda: NOW)
    client._exchange = stub  # type: ignore[assignment]
    assert await client.fetch_candles(PAIR, TF) == []
    assert stub.limit == BINANCE_PAGE_SIZE
