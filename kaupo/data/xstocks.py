"""Public Kraken xStocks data and streaming trade-to-candle reconstruction.

This client is separate from the crypto/live adapter. Kraken discovery uses
aclass_base, whereas its market-data endpoints require asset_class. Venue
symbols keep their original case; stored Pair values retain Kaupo's uppercase
convention. No credentials or order endpoints are used.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.candles import upsert_candles
from kaupo.data.ccxt_client import CLOSE_GRACE, CcxtExchangeClient
from kaupo.domain import Candle, Pair, Timeframe, TradeTick

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_seconds(value: datetime) -> Decimal:
    if value.tzinfo is None:
        raise ValueError("Use a timezone-aware timestamp")
    delta = value - EPOCH
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000


@dataclass(frozen=True)
class XStockMarket:
    pair: Pair
    api_pair: str
    status: str
    tick_size: str
    order_min: str | None = None
    cost_min: str | None = None
    lot_decimals: int | None = None
    lot_multiplier: int | None = None
    lot: str | None = None


@dataclass(frozen=True)
class XStockTrade:
    id: int
    ts: Decimal
    price: Decimal
    size: Decimal
    side: str

    @property
    def key(self) -> tuple[Decimal, int]:
        return self.ts, self.id


class XStocksClient:
    """Rate-limited, unauthenticated access to Kraken's tokenized-asset class."""

    asset_class = "tokenized_asset"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        request_interval: float = 1.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url="https://api.kraken.com/0/public/", timeout=30, transport=transport
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._interval = request_interval
        self._last_request = -math.inf
        self._lock = asyncio.Lock()
        self._markets: dict[Pair, XStockMarket] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        async with self._lock:
            delay = self._interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()
            response = await self._http.get(endpoint, params=params)
        response.raise_for_status()
        # Preserve fractional trade timestamps before assigning candle buckets.
        body = json.loads(response.text, parse_float=Decimal)
        if body.get("error"):
            raise ValueError(f"Kraken {endpoint}: {', '.join(body['error'])}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"Kraken {endpoint}: missing result")
        return result

    async def identity_metadata(self) -> dict[str, Any]:
        """Return internal pair and asset IDs for explicit account identity mapping."""
        try:
            return await self._get(
                "AssetPairs", {"aclass_base": "tokenized_asset", "execution_venue": "international"}
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"Kraken identity metadata request failed ({type(exc).__name__})") from None

    async def markets(self, country_code: str | None = None) -> list[XStockMarket]:
        params: dict[str, str | int] = {"aclass_base": "tokenized_asset", "execution_venue": "international"}
        if country_code:
            params["country_code"] = country_code.upper()
        raw = await self._get("AssetPairs", params)
        markets: dict[Pair, XStockMarket] = {}
        for row in raw.values():
            if (
                row.get("aclass_base") != "tokenized_asset"
                or row.get("execution_venue", "international") != "international"
            ):
                continue
            pair = Pair.parse(row["wsname"])
            market = XStockMarket(
                pair,
                row["altname"],
                row["status"],
                row["tick_size"],
                row.get("ordermin"),
                row.get("costmin"),
                row.get("lot_decimals"),
                row.get("lot_multiplier"),
                row.get("lot"),
            )
            if pair in markets and markets[pair] != market:
                raise ValueError(f"Conflicting Kraken metadata for {pair}")
            markets[pair] = market  # Kraken can return legacy and display aliases
        if country_code is None:
            self._markets = markets
        return sorted(markets.values(), key=lambda market: str(market.pair))

    async def _symbol(self, pair: Pair) -> str:
        if self._markets is None:
            await self.markets()
        assert self._markets is not None
        market = self._markets.get(pair)
        if market is None:
            raise ValueError(f"{pair} is not in Kraken's xStocks market list")
        return market.api_pair

    async def validate_pairs(self, pairs: list[Pair]) -> None:
        """Resolve the whole universe before a shadow run can start."""
        for pair in pairs:
            await self._symbol(pair)

    async def fetch_candles(
        self,
        pair: Pair,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Latest complete page for startup/polling; no deep-history guarantee."""
        if since is not None and since.utcoffset() is None:
            raise ValueError("since must include a timezone")
        if limit is not None and limit <= 0:
            return []
        candles = sorted(await self.recent_candles(pair, timeframe), key=lambda candle: candle.ts)
        if since is not None:
            candles = [candle for candle in candles if candle.ts >= since]
        return candles[-limit:] if limit is not None else candles

    async def recent_candles(self, pair: Pair, timeframe: Timeframe) -> list[Candle]:
        symbol = await self._symbol(pair)
        result = await self._get(
            "OHLC", {"pair": symbol, "asset_class": "tokenized_asset", "interval": timeframe.seconds // 60}
        )
        rows = self._rows(result)
        candles = []
        for row in rows:
            ts = EPOCH + timedelta(seconds=int(row[0]))
            if ts + timedelta(seconds=timeframe.seconds) + CLOSE_GRACE > self._now():
                continue
            o, h, low, c, volume = (float(row[i]) for i in (1, 2, 3, 4, 6))
            if not CcxtExchangeClient._valid(o, h, low, c, volume):
                raise ValueError(f"Invalid xStocks candle for {pair} at {ts}")
            if volume == 0 and int(row[7]) == 0:
                continue  # Kraken fills some empty periods with the previous price.
            candles.append(Candle(pair, timeframe, ts, o, h, low, c, volume, exchange="kraken"))
        return candles

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[Any]:
        markets = [rows for key, rows in result.items() if key != "last"]
        if len(markets) != 1 or not isinstance(markets[0], list):
            raise ValueError("Kraken returned an unexpected market-data response")
        return markets[0]

    async def trade_page(self, pair: Pair, cursor: str) -> tuple[list[XStockTrade], str]:
        symbol = await self._symbol(pair)
        result = await self._get(
            "Trades",
            {
                "pair": symbol,
                "asset_class": "tokenized_asset",
                "since": cursor,
                "count": 1000,
            },
        )
        next_cursor = str(result.get("last", ""))
        if not next_cursor.isdigit():
            raise ValueError("Kraken trades response has no valid pagination cursor")
        trades = []
        for row in self._rows(result):
            if len(row) < 7 or type(row[6]) is not int or row[6] < 0 or row[3] not in ("b", "s"):
                raise ValueError("Invalid xStocks trade identity")
            trade = XStockTrade(
                row[6],
                Decimal(str(row[2])),
                Decimal(row[0]),
                Decimal(row[1]),
                "buy" if row[3] == "b" else "sell",
            )
            if any(not v.is_finite() or v <= 0 for v in (trade.ts, trade.price, trade.size)):
                raise ValueError("Invalid xStocks trade price, size, or timestamp")
            trades.append(trade)
        if trades != sorted(trades, key=lambda trade: trade.key):
            raise ValueError("Kraken trades are not in timestamp and trade-ID order")
        return trades, next_cursor

    async def historical_candles(
        self,
        pair: Pair,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[Candle]:
        """Stream complete UTC candles; preserve gaps and deduplicate by trade ID.

        Requests with partial boundary buckets exclude those buckets. The last
        bucket stays in memory until a later trade or the end of history proves
        it complete. API failures never write that partial bucket.
        """
        seconds = timeframe.seconds
        start_s, end_s = epoch_seconds(start), epoch_seconds(end)
        if start_s >= end_s:
            raise ValueError("The start must be before the end")
        first = math.ceil(start_s / seconds) * seconds
        stop = math.floor(min(end_s, epoch_seconds(self._now() - CLOSE_GRACE)) / seconds) * seconds
        if first >= stop:
            return
        bucket: int | None = None
        prices: list[Decimal] = []  # open, high, low, close, volume

        def candle() -> Candle:
            assert bucket is not None
            o, h, low, c, volume = map(float, prices)
            if not CcxtExchangeClient._valid(o, h, low, c, volume):
                raise ValueError("Reconstructed candle exceeds the supported numeric range")
            return Candle(
                pair, timeframe, EPOCH + timedelta(seconds=bucket), o, h, low, c, volume, exchange="kraken"
            )

        async for trade in self._trades_between(pair, Decimal(first), Decimal(stop)):
            trade_bucket = int(trade.ts // seconds) * seconds
            if bucket != trade_bucket:
                if bucket is not None:
                    yield candle()
                bucket = trade_bucket
                prices = [trade.price, trade.price, trade.price, trade.price, trade.size]
            else:
                prices[1] = max(prices[1], trade.price)
                prices[2] = min(prices[2], trade.price)
                prices[3] = trade.price
                prices[4] += trade.size
        if bucket is not None:
            yield candle()

    async def historical_trades(
        self, pair: Pair, start: datetime, end: datetime
    ) -> AsyncIterator[XStockTrade]:
        """Unique public trades in [start, end), with exact source timestamps."""
        first, stop = epoch_seconds(start), epoch_seconds(end)
        if first >= stop:
            raise ValueError("The start must be before the end")
        stop = min(stop, epoch_seconds(self._now()))
        if first < stop:
            async for trade in self._trades_between(pair, first, stop):
                yield trade

    async def _trades_between(self, pair: Pair, first: Decimal, stop: Decimal) -> AsyncIterator[XStockTrade]:
        # Include boundary trades even when "since" is exclusive.
        cursor = str(int(first * 1_000_000_000) - 1)
        last_key: tuple[Decimal, int] | None = None
        previous: dict[int, XStockTrade] = {}
        while True:
            page, next_cursor = await self.trade_page(pair, cursor)
            unique: dict[int, XStockTrade] = {}
            for trade in page:
                old = unique.get(trade.id) or previous.get(trade.id)
                if old is not None:
                    if old != trade:
                        raise ValueError("Kraken changed a trade across pagination")
                    continue
                if last_key is not None and trade.key <= last_key:
                    raise ValueError("Kraken returned older trades outside the page overlap")
                unique[trade.id] = trade
            for trade in unique.values():
                last_key = trade.key
                if trade.ts >= stop:
                    return
                if trade.ts >= first:
                    yield trade
            if not page or (not unique and next_cursor == cursor):
                return
            if int(next_cursor) <= int(cursor):
                raise ValueError("Kraken trade cursor did not advance")
            previous = {trade.id: trade for trade in page}
            cursor = next_cursor


async def ingest_xstock_trades(
    client: XStocksClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    start: datetime,
    end: datetime,
) -> int:
    """Commit bounded batches of ID-preserving ticks; retries are idempotent."""
    from kaupo.data.trades import upsert_trade_ticks

    batch = []
    total = 0
    async for trade in client.historical_trades(pair, start, end):
        price, size = float(trade.price), float(trade.size)
        if not all(math.isfinite(value) and value > 0 for value in (price, size)):
            raise ValueError("xStocks trade exceeds the supported numeric range")
        # Floor to database microseconds; rounding can move a trade into the next bucket.
        ts = EPOCH + timedelta(microseconds=int(trade.ts * 1_000_000))
        batch.append(TradeTick("kraken", str(pair), ts, price, size, trade.side, str(trade.id)))
        if len(batch) >= 100:
            async with sessionmaker() as session:
                total += await upsert_trade_ticks(session, batch)
                await session.commit()
            batch.clear()
    if batch:
        async with sessionmaker() as session:
            total += await upsert_trade_ticks(session, batch)
            await session.commit()
    return total


async def ingest_xstock_candles(
    client: XStocksClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    recent: bool = False,
) -> int:
    """Upsert complete candles; repeated imports replace the same candle keys."""
    if start >= end:
        raise ValueError("The start must be before the end")
    total = 0
    if recent:
        candles = [
            c
            for c in await client.recent_candles(pair, timeframe)
            if start <= c.ts and c.ts + timedelta(seconds=timeframe.seconds) <= end
        ]
        async with sessionmaker() as session:
            total = await upsert_candles(session, candles)
            await session.commit()
        return total
    # Commit complete candles in small batches. No raw-tick retention or
    # millisecond/price deduplication can change the reconstructed volumes.
    batch = []
    async for candle in client.historical_candles(pair, timeframe, start, end):
        batch.append(candle)
        if len(batch) >= 100:
            async with sessionmaker() as session:
                total += await upsert_candles(session, batch)
                await session.commit()
            batch.clear()
    if batch:
        async with sessionmaker() as session:
            total += await upsert_candles(session, batch)
            await session.commit()
    return total
