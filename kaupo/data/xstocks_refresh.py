"""Bounded public xStocks refresh with explicit calendar coverage evidence."""

import math
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.data.candles import get_candles
from kaupo.data.ccxt_client import CLOSE_GRACE
from kaupo.data.xstocks import XStocksClient, ingest_xstock_candles
from kaupo.domain import Pair, Timeframe
from kaupo.market_calendar import TradingSchedule


class RefreshInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair: str
    timeframes: list[Timeframe] = Field(min_length=1)
    trading_schedule: TradingSchedule

    @field_validator("pair")
    @classmethod
    def normalized_pair(cls, value: str) -> str:
        return str(Pair.parse(value))

    @field_validator("timeframes")
    @classmethod
    def unique_timeframes(cls, value: list[Timeframe]) -> list[Timeframe]:
        if len(set(value)) != len(value):
            raise ValueError("timeframes must not contain duplicates")
        return value


class RefreshConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=3, ge=1, le=30, strict=True)
    instruments: list[RefreshInstrument] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_pairs(self) -> Self:
        pairs = [instrument.pair for instrument in self.instruments]
        if len(set(pairs)) != len(pairs):
            raise ValueError("instruments must not contain duplicate pairs")
        return self


class Coverage(BaseModel):
    status: Literal["complete", "scheduled_closed", "missing", "schedule_mismatch"]
    expected: int
    observed: int
    missing: int
    unexpected: int
    first_observed: datetime | None
    last_observed: datetime | None
    missing_sample: list[datetime]


class RefreshResult(BaseModel):
    pair: str
    timeframe: str
    start: datetime
    end: datetime
    trading_schedule: TradingSchedule
    imported: int = 0
    coverage: Coverage | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and self.coverage is not None
            and self.coverage.status in ("complete", "scheduled_closed")
        )


def candle_coverage(
    timestamps: list[datetime],
    timeframe: Timeframe,
    schedule: TradingSchedule,
    start: datetime,
    end: datetime,
) -> Coverage:
    """Compare stored observations with UTC buckets that overlap open sessions.

    Missing bars are evidence gaps, not proof of a source outage: a quiet
    instrument can have no trades in an open bucket. No candles are invented.
    """
    if (
        start.utcoffset() is None
        or end.utcoffset() is None
        or any(ts.utcoffset() is None for ts in timestamps)
    ):
        raise ValueError("coverage timestamps must include a timezone")
    seconds = timeframe.seconds
    if start >= end or start.timestamp() % seconds or end.timestamp() % seconds:
        raise ValueError("coverage requires an ordered range aligned to candle boundaries")
    expected = set()
    ts = start.astimezone(UTC)
    step = timedelta(seconds=seconds)
    while ts < end:
        if schedule.open_elapsed(ts, ts + step) > timedelta(0):
            expected.add(ts)
        ts += step
    observed = {ts.astimezone(UTC) for ts in timestamps if start <= ts < end}
    missing = sorted(expected - observed)
    unexpected = observed - expected
    status: Literal["complete", "scheduled_closed", "missing", "schedule_mismatch"] = "complete"
    if unexpected:
        status = "schedule_mismatch"
    elif missing:
        status = "missing"
    elif not expected:
        status = "scheduled_closed"
    return Coverage(
        status=status,
        expected=len(expected),
        observed=len(observed),
        missing=len(missing),
        unexpected=len(unexpected),
        first_observed=min(observed) if observed else None,
        last_observed=max(observed) if observed else None,
        missing_sample=missing[:10],
    )


async def refresh_xstocks(
    config: RefreshConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: XStocksClient,
    *,
    now: datetime,
) -> list[RefreshResult]:
    """Refresh each target independently; return API errors even if cached data exists."""
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    results = []
    for instrument in config.instruments:
        for timeframe in instrument.timeframes:
            end = datetime.fromtimestamp(
                math.floor((now - CLOSE_GRACE).timestamp() / timeframe.seconds) * timeframe.seconds, UTC
            )
            start = end - timedelta(days=config.days)
            result = RefreshResult(
                pair=instrument.pair,
                timeframe=timeframe.value,
                start=start,
                end=end,
                trading_schedule=instrument.trading_schedule,
            )
            try:
                pair = Pair.parse(instrument.pair)
                # The dedicated client resolves the pair before every first fetch.
                # A closed schedule does not skip fetches or hide API failures.
                result.imported = await ingest_xstock_candles(
                    client, sessionmaker, pair, timeframe, start, end, recent=True
                )
                async with sessionmaker() as session:
                    candles = await get_candles(session, pair, timeframe, start, end, exchange="kraken")
                result.coverage = candle_coverage(
                    [candle.ts for candle in candles], timeframe, instrument.trading_schedule, start, end
                )
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
            results.append(result)
    return results
