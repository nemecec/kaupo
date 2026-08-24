"""KrakenClient boundary behavior with a stubbed ccxt exchange."""

from datetime import UTC, datetime, timedelta

from kaupo.data.kraken import KrakenClient
from kaupo.domain import Pair, Timeframe

PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1
NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=UTC)


class StubExchange:
    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):  # type: ignore[no-untyped-def]
        return self.rows

    async def close(self) -> None:
        pass


def make_client(rows: list[list[float]]) -> KrakenClient:
    client = KrakenClient(now=lambda: NOW)
    client._exchange = StubExchange(rows)  # type: ignore[assignment]
    return client


def ohlcv(ts: datetime, price: float = 100.0) -> list[float]:
    return [int(ts.timestamp() * 1000), price, price + 1, price - 1, price, 1.0]


async def test_in_progress_candle_dropped() -> None:
    # one closed candle (11:00, closes 12:00 + grace) and one in-progress (12:00)
    rows = [ohlcv(NOW - timedelta(hours=1)), ohlcv(NOW)]
    candles = await make_client(rows).fetch_candles(PAIR, TF)
    assert len(candles) == 1
    assert candles[0].ts == NOW - timedelta(hours=1)


async def test_close_grace_margin() -> None:
    # candle closes 1s in the future: without the grace margin it would look
    # closed; the 2s grace keeps it out
    rows = [ohlcv(NOW - timedelta(hours=1) + timedelta(seconds=1))]
    candles = await make_client(rows).fetch_candles(PAIR, TF)
    assert candles == []


async def test_invalid_candles_dropped() -> None:
    ts = NOW - timedelta(hours=2)
    bad_rows = [
        [int(ts.timestamp() * 1000), 0, 1, 0, 1, 1],  # zero open
        [int(ts.timestamp() * 1000), 100, 90, 110, 100, 1],  # high < low
        [int(ts.timestamp() * 1000), 100, 101, 99, 105, 1],  # close outside range
        [int(ts.timestamp() * 1000), float("nan"), 101, 99, 100, 1],  # NaN
        ohlcv(ts, 100.0),  # the only valid one
    ]
    candles = await make_client(bad_rows).fetch_candles(PAIR, TF)
    assert len(candles) == 1
    assert candles[0].open == 100.0
