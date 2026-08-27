"""Generic ccxt market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import ccxt.async_support as ccxt

from kaupo.domain import Candle, Pair, Timeframe, TradeTick

log = logging.getLogger(__name__)

# margin for clock skew / exchange-side finalization before a candle counts as closed
CLOSE_GRACE = timedelta(seconds=2)

TRADES_PAGE_SIZE = 1000  # Kraken and Binance serve at most 1000 trades per call


def _normalized_side(entry: dict[str, Any]) -> str | None:
    side = entry.get("side")
    return side if side in ("buy", "sell") else None


def parse_trade_ticks(
    raw: list[dict[str, Any]],
    exchange: str,
    pair: Pair,
    side_of: Callable[[dict[str, Any]], str | None] = _normalized_side,
) -> list[TradeTick]:
    """Normalize ccxt trade structures to ticks; malformed rows are dropped."""
    ticks = []
    for entry in raw:
        ts_ms = entry.get("timestamp")
        price = entry.get("price")
        amount = entry.get("amount")
        side = side_of(entry)
        if (
            not ts_ms
            or ts_ms <= 0
            or price is None
            or not math.isfinite(price)
            or price <= 0
            or amount is None
            or not math.isfinite(amount)
            or amount <= 0
            or side is None
        ):
            log.warning("Dropping malformed trade row %s %s: %s", exchange, pair, entry)
            continue
        ticks.append(
            TradeTick(
                exchange=exchange,
                pair=str(pair),
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                price=float(price),
                size=float(amount),
                side=side,
            )
        )
    return ticks


class CcxtExchangeClient:
    """Thin async wrapper around a ccxt exchange, returning domain candles.

    Only *complete* candles are ever returned (the in-progress candle is dropped).
    """

    #: Window one fetch_trades page covers, when the venue pages by windows
    #: instead of a continuous cursor (Binance aggTrades: one hour). An empty
    #: page there means an empty window, not the live edge.
    trades_page_window: timedelta | None = None

    def __init__(
        self,
        exchange_id: str,
        page_size: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.exchange_id = exchange_id
        self.page_size = page_size
        self._exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        self._now = now or (lambda: datetime.now(UTC))
        self._trades_cursor: str | None = None
        self._trade_side: Callable[[dict[str, Any]], str | None] = _normalized_side

    @property
    def trades_cursor(self) -> str | None:
        """Opaque forward-paging cursor of the last fetch_trades call.

        Only venues whose paging needs an exchange cursor set it (Kraken's
        nanosecond ``last``); it stays None elsewhere.
        """
        return self._trades_cursor

    @staticmethod
    def _valid(o: float, h: float, low: float, c: float, v: float) -> bool:
        values = (o, h, low, c, v)
        return (
            all(math.isfinite(x) for x in values)
            and o > 0
            and h > 0
            and low > 0
            and c > 0
            and h >= low
            and low <= o <= h
            and low <= c <= h
            and v >= 0
        )

    async def close(self) -> None:
        await self._exchange.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def fetch_candles(
        self,
        pair: Pair,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        if limit is None:
            limit = self.page_size
        since_ms = int(since.timestamp() * 1000) if since else None
        ohlcv = await self._exchange.fetch_ohlcv(str(pair), timeframe.value, since=since_ms, limit=limit)
        now = self._now()
        candles = []
        for ts_ms, o, h, low, c, v in ohlcv:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            if ts + timedelta(seconds=timeframe.seconds) + CLOSE_GRACE > now:
                continue  # in-progress candle
            if not self._valid(o, h, low, c, v):
                log.warning(
                    "Dropping invalid candle %s %s %s: %s", pair, timeframe.value, ts, (o, h, low, c, v)
                )
                continue
            candles.append(
                Candle(
                    exchange=self.exchange_id,
                    pair=pair,
                    timeframe=timeframe,
                    ts=ts,
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=float(v),
                )
            )
        return candles

    async def fetch_trades(
        self,
        pair: Pair,
        since: datetime | None = None,
        limit: int | None = None,
        *,
        cursor: str | None = None,
    ) -> list[TradeTick]:
        """One page of public trades at or after ``since``, ascending.

        ``cursor`` is ignored here; only venues with cursor-based paging
        (Kraken) use it. Malformed rows are dropped.
        """
        if limit is None:
            limit = TRADES_PAGE_SIZE
        since_ms = int(since.timestamp() * 1000) if since else None
        raw = await self._exchange.fetch_trades(str(pair), since=since_ms, limit=limit)
        return parse_trade_ticks(raw, self.exchange_id, pair, side_of=self._trade_side)
