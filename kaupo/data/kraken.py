"""Kraken market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from kaupo.data.ccxt_client import TRADES_PAGE_SIZE, CcxtExchangeClient, parse_trade_ticks
from kaupo.domain import Pair, TradeTick

KRAKEN_PAGE_SIZE = 720  # Kraken returns at most 720 OHLC entries per call


def _response_cursor(raw: list[dict[str, Any]]) -> str | None:
    """The response-level nanosecond ``last`` cursor of a Kraken trades page.

    ccxt appends it to the last trade's info list (index 7, after the raw
    7-field trade). Absent on empty or malformed pages.
    """
    if raw:
        info = raw[-1].get("info")
        if isinstance(info, list) and len(info) > 7:
            return str(info[7])
    return None


class KrakenClient(CcxtExchangeClient):
    """Kraken candles; only the newest ``KRAKEN_PAGE_SIZE`` per timeframe are served."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__("kraken", KRAKEN_PAGE_SIZE, now)

    async def fetch_trades(
        self,
        pair: Pair,
        since: datetime | None = None,
        limit: int | None = None,
        *,
        cursor: str | None = None,
    ) -> list[TradeTick]:
        """One page of public trades, paged by Kraken's nanosecond cursor.

        ccxt's own ``since`` handling is broken for Kraken: it converts ms to
        seconds while Kraken expects a nanosecond cursor, and silently serves
        2013 data. The cursor goes through ``params`` instead. The explicit
        ``cursor`` (the previous page's ``last``, exposed as ``trades_cursor``)
        wins; ``since`` only seeds the first page. Trade times share the same
        millisecond in bursts, so paging by the last tick's ts would skip
        same-ms trades — the response cursor does not.
        """
        if limit is None:
            limit = TRADES_PAGE_SIZE
        if cursor is None and since is not None:
            cursor = str(int(since.timestamp() * 1000) * 1_000_000)
        params = {"since": cursor} if cursor else {}
        raw = await self._exchange.fetch_trades(str(pair), limit=limit, params=params)
        self._trades_cursor = _response_cursor(raw)
        return parse_trade_ticks(raw, self.exchange_id, pair)
