"""UTC-midnight equity valuations for scheduled instruments."""

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from kaupo.domain import Timeframe
from kaupo.market_calendar import TradingSchedule


def daily_equity(
    equity: list[tuple[datetime, float]],
    timeframe: Timeframe,
    schedule: TradingSchedule,
    valuation_end: datetime | None = None,
) -> tuple[list[tuple[datetime, float]], str | None]:
    """Carry observed closing valuations across closures, never across missing bars.

    Snapshot timestamps are candle OPEN times. A value becomes available one
    timeframe later. Only complete UTC-day intervals enter the return series.
    ``valuation_end`` permits carrying a final valuation through a closure,
    but not through an unobserved trading interval.
    """
    if any(ts.utcoffset() is None for ts, _ in equity):
        return [], "naive_equity_timestamp"
    points = [(ts.astimezone(UTC), value) for ts, value in equity]
    if not points:
        return [], "insufficient_daily_returns"
    if any(not math.isfinite(value) or value <= 0 for _, value in points):
        return [], "invalid_return_equity"
    if any(right[0] <= left[0] for left, right in pairwise(points)):
        return [], "unordered_equity_timestamps"
    if any(ts.timestamp() % timeframe.seconds != 0 for ts, _ in points):
        return [], "unaligned_equity_timestamps"
    step = timedelta(seconds=timeframe.seconds)
    if valuation_end is not None and valuation_end.utcoffset() is None:
        return [], "naive_valuation_end"
    end = valuation_end.astimezone(UTC) if valuation_end is not None else points[-1][0] + step
    points = [(ts, value) for ts, value in points if ts + step <= end]
    if not points:
        return [], "insufficient_daily_returns"
    first_close = points[0][0] + step
    first = first_close.replace(hour=0, minute=0, second=0, microsecond=0)
    if first < first_close:
        first += timedelta(days=1)
    last = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if first > last:
        return [], "insufficient_daily_returns"

    # Include the first anchor's delivery window: an already stale anchor must
    # not make the first daily return appear well supported.
    previous_close = first_close
    for opened, _ in points[1:]:
        if schedule.open_elapsed(previous_close, min(opened, last), candle_seconds=timeframe.seconds):
            return [], "missing_scheduled_candles"
        previous_close = opened + step
        if previous_close >= last:
            break
    if schedule.open_elapsed(previous_close, last, candle_seconds=timeframe.seconds):
        return [], "missing_scheduled_candles"

    result = []
    index = 0
    boundary = first
    while boundary <= last:
        while index + 1 < len(points) and points[index + 1][0] + step <= boundary:
            index += 1
        result.append((boundary, points[index][1]))
        boundary += timedelta(days=1)
    return result, None
