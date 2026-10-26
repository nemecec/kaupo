"""Shared candle interface for crypto and explicitly selected xStocks sources."""

from datetime import datetime
from typing import Literal, Protocol

from kaupo.domain import Candle, Pair, Timeframe

AssetClass = Literal["currency", "tokenized_asset"]


class CandleClient(Protocol):
    async def fetch_candles(
        self,
        pair: Pair,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...
