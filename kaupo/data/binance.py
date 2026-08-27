"""Binance market-data client. Public endpoints only — no API keys needed."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import ccxt.async_support as ccxt

from kaupo.data.ccxt_client import CcxtExchangeClient
from kaupo.domain import FundingRate

log = logging.getLogger(__name__)

BINANCE_PAGE_SIZE = 1500  # Binance returns at most 1500 klines per call
BINANCE_FUNDING_PAGE_SIZE = 1000  # Binance returns at most 1000 funding entries per call


def _agg_side(entry: dict[str, Any]) -> str | None:
    """Side of an aggTrade from the buyer-is-maker flag (m).

    The aggressor (taker) sets the tick side: when the buyer is the maker,
    the seller is the aggressor, so the tick is a "sell".
    """
    info = entry.get("info")
    if isinstance(info, dict) and "m" in info:
        return "sell" if info["m"] else "buy"
    side = entry.get("side")
    return side if side in ("buy", "sell") else None


class BinanceClient(CcxtExchangeClient):
    """Binance candles; serves deep history (paginates forward from ``since``).

    Also serves perpetual-futures funding history from the separate
    ``binanceusdm`` market (one dominant USDT perpetual per base asset).
    """

    # one aggTrades page covers a one-hour window (ccxt sets endTime = since + 1h)
    trades_page_window = timedelta(hours=1)

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__("binance", BINANCE_PAGE_SIZE, now)
        # aggTrades supports startTime-based paging; recent-trades does not
        self._exchange.options["fetchTradesMethod"] = "publicGetAggTrades"
        # aggTrade side comes from the buyer-is-maker flag (m), not ccxt's field
        self._trade_side = _agg_side
        self._futures = ccxt.binanceusdm({"enableRateLimit": True})

    async def close(self) -> None:
        await super().close()
        await self._futures.close()

    async def fetch_funding_rates(
        self,
        base_asset: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingRate]:
        """Funding history of the base asset's USDT-margined perpetual, ascending.

        Unknown perps surface as ccxt ``BadSymbol``; malformed rows are dropped.
        """
        if limit is None:
            limit = BINANCE_FUNDING_PAGE_SIZE
        base = base_asset.strip().upper()
        if not base:
            raise ValueError(f"Invalid base asset {base_asset!r}")
        since_ms = int(since.timestamp() * 1000) if since else None
        history = await self._futures.fetch_funding_rate_history(
            f"{base}/USDT:USDT", since=since_ms, limit=limit
        )
        rates = []
        for entry in history:
            ts_ms = entry.get("timestamp")
            rate = entry.get("fundingRate")
            if not ts_ms or ts_ms <= 0 or rate is None or not math.isfinite(rate):
                log.warning("Dropping malformed funding row %s: %s", base, entry)
                continue
            rates.append(
                FundingRate(
                    exchange="binance",
                    base_asset=base,
                    ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                    rate=float(rate),
                )
            )
        return rates
