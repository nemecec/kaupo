"""fetch_trades on the venue clients: ccxt mapping, cursors, malformed rows.

Kraken assertions pin the workaround for the broken ccxt ``since`` (it
converts ms to seconds while Kraken wants a nanosecond cursor): the cursor
must go through ``params`` and ccxt's own ``since`` must stay None.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from kaupo.data.binance import BinanceClient
from kaupo.data.ccxt_client import TRADES_PAGE_SIZE
from kaupo.data.kraken import KrakenClient
from kaupo.domain import Pair

PAIR = Pair.parse("BTC/EUR")
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)


class StubExchange:
    """Stands in for the ccxt exchange: serves canned trade pages."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls: list[tuple[Any, Any, Any]] = []  # (since, limit, params) per call

    async def fetch_trades(self, symbol, since=None, limit=None, params=None):  # type: ignore[no-untyped-def]
        self.calls.append((since, limit, params))
        if not self.pages:
            return []
        return self.pages.pop(0)

    async def close(self) -> None:
        pass


def kraken_trade(ts: datetime, price: float, size: float, side: str, cursor: str | None) -> dict:
    """A ccxt-normalized Kraken trade; info is the raw 7-field array plus the
    response-level ``last`` cursor ccxt appends to the last trade."""
    raw_side = "b" if side == "buy" else "s"
    info: list[Any] = [str(price), str(size), ts.timestamp(), raw_side, "m", "", 109085779]
    if cursor is not None:
        info.append(cursor)
    return {
        "info": info,
        "timestamp": int(ts.timestamp() * 1000),
        "price": price,
        "amount": size,
        "side": side,
        "id": "109085779",
    }


def binance_trade(ts: datetime, price: float, size: float, buyer_maker: bool) -> dict:
    """A ccxt-normalized Binance aggTrade; info is the raw aggTrade dict."""
    return {
        "info": {
            "a": 26129,
            "p": str(price),
            "q": str(size),
            "T": int(ts.timestamp() * 1000),
            "m": buyer_maker,
        },
        "timestamp": int(ts.timestamp() * 1000),
        "price": price,
        "amount": size,
        "side": None,  # the client derives the side from the m flag, not this field
        "id": "26129",
    }


def make_kraken(pages: list[list[dict]]) -> tuple[KrakenClient, StubExchange]:
    client = KrakenClient(now=lambda: NOW)
    stub = StubExchange(pages)
    client._exchange = stub  # type: ignore[assignment]
    return client, stub


def make_binance(pages: list[list[dict]]) -> tuple[BinanceClient, StubExchange]:
    client = BinanceClient(now=lambda: NOW)
    stub = StubExchange(pages)
    client._exchange = stub  # type: ignore[assignment]
    return client, stub


class TestKrakenFetchTrades:
    async def test_since_goes_through_params_not_ccxt(self) -> None:
        page = [kraken_trade(BASE, 100.0, 0.1, "buy", cursor="1760000000000000001")]
        client, stub = make_kraken([page])

        ticks = await client.fetch_trades(PAIR, since=BASE)

        assert len(ticks) == 1
        since, limit, params = stub.calls[0]
        assert since is None  # ccxt's broken ms->s conversion must not engage
        assert limit == TRADES_PAGE_SIZE
        assert params == {"since": str(int(BASE.timestamp() * 1000) * 1_000_000)}

    async def test_response_cursor_is_exposed_and_pages_forward(self) -> None:
        page1 = [
            kraken_trade(BASE, 100.0, 0.1, "buy", cursor=None),
            kraken_trade(BASE, 100.5, 0.2, "sell", cursor="1760000000123456789"),
        ]
        page2 = [kraken_trade(BASE + timedelta(milliseconds=5), 101.0, 0.1, "buy", cursor=None)]
        client, stub = make_kraken([page1, page2])

        await client.fetch_trades(PAIR, since=BASE)
        assert client.trades_cursor == "1760000000123456789"

        await client.fetch_trades(PAIR, since=BASE + timedelta(milliseconds=1), cursor=client.trades_cursor)
        since, _, params = stub.calls[1]
        assert since is None
        assert params == {"since": "1760000000123456789"}  # the response cursor wins over since

    async def test_cursor_is_none_when_the_response_lacks_it(self) -> None:
        # a 7-field info array without the appended cursor
        client, _ = make_kraken([[kraken_trade(BASE, 100.0, 0.1, "buy", cursor=None)]])
        await client.fetch_trades(PAIR, since=BASE)
        assert client.trades_cursor is None

    async def test_no_since_no_params(self) -> None:
        client, stub = make_kraken([[kraken_trade(BASE, 100.0, 0.1, "buy", cursor="1")]])
        await client.fetch_trades(PAIR)
        assert stub.calls[0] == (None, TRADES_PAGE_SIZE, {})

    async def test_ticks_parsed_and_malformed_rows_dropped(self) -> None:
        good = kraken_trade(BASE, 100.0, 0.1, "buy", cursor=None)
        rows = [
            good,
            {**kraken_trade(BASE, 100.0, 0.1, "buy", None), "timestamp": None},  # no ts
            {**kraken_trade(BASE, 100.0, 0.1, "buy", None), "price": 0.0},  # bad price
            {**kraken_trade(BASE, 100.0, 0.1, "buy", None), "amount": -1.0},  # bad size
            {**kraken_trade(BASE, 100.0, 0.1, "buy", None), "side": "hold"},  # unknown side
            # ccxt appends the response cursor to the page's last trade
            kraken_trade(BASE, 100.0, 0.1, "buy", cursor="9"),
        ]
        client, _ = make_kraken([rows])
        ticks = await client.fetch_trades(PAIR, since=BASE)
        assert len(ticks) == 2
        first = ticks[0]
        assert (first.exchange, first.pair, first.ts, first.price, first.size, first.side) == (
            "kraken",
            "BTC/EUR",
            BASE,
            100.0,
            0.1,
            "buy",
        )
        assert client.trades_cursor == "9"


class TestBinanceFetchTrades:
    def test_agg_trades_method_is_pinned(self) -> None:
        client = BinanceClient(now=lambda: NOW)
        assert client._exchange.options["fetchTradesMethod"] == "publicGetAggTrades"
        assert client.trades_page_window == timedelta(hours=1)

    async def test_since_is_passed_to_ccxt_in_ms(self) -> None:
        client, stub = make_binance([[binance_trade(BASE, 100.0, 0.1, buyer_maker=False)]])
        await client.fetch_trades(PAIR, since=BASE)
        since, limit, params = stub.calls[0]
        assert since == int(BASE.timestamp() * 1000)
        assert limit == TRADES_PAGE_SIZE
        assert params is None

    async def test_side_comes_from_the_buyer_maker_flag(self) -> None:
        rows = [
            binance_trade(BASE, 100.0, 0.1, buyer_maker=True),  # seller is the aggressor
            binance_trade(BASE + timedelta(seconds=1), 100.5, 0.2, buyer_maker=False),
        ]
        client, _ = make_binance([rows])
        ticks = await client.fetch_trades(PAIR, since=BASE)
        assert [t.side for t in ticks] == ["sell", "buy"]
        assert ticks[0].exchange == "binance"
        assert ticks[0].size == 0.1

    async def test_rows_without_a_mappable_side_are_dropped(self) -> None:
        rows = [
            {**binance_trade(BASE, 100.0, 0.1, buyer_maker=True), "info": {}},  # no m, no side
            binance_trade(BASE, 100.0, 0.1, buyer_maker=False),
        ]
        client, _ = make_binance([rows])
        ticks = await client.fetch_trades(PAIR, since=BASE)
        assert [t.side for t in ticks] == ["buy"]
