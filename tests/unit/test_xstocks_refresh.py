"""Calendar coverage distinguishes missing evidence, closures, and schedule errors."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.data.xstocks_refresh import RefreshConfig, candle_coverage
from kaupo.domain import Timeframe
from kaupo.market_calendar import TradingSchedule

FRIDAY = datetime(2026, 10, 23, tzinfo=UTC)
WEEK = {i: [{"start": 0, "end": 1440}] for i in range(5)}


def schedule(**changes):
    return TradingSchedule.model_validate({"timezone": "UTC", "week": WEEK, **changes})


def config(**changes):
    return RefreshConfig.model_validate(
        {
            "instruments": [
                {
                    "pair": "AAPLx/USD",
                    "timeframes": ["1h"],
                    "trading_schedule": schedule().model_dump(mode="json"),
                }
            ],
            **changes,
        }
    )


def test_weekend_and_open_gap_are_distinct():
    saturday = FRIDAY + timedelta(days=1)
    monday = FRIDAY + timedelta(days=3)
    closed = candle_coverage([], Timeframe.H1, schedule(), saturday, monday)
    assert closed.status == "scheduled_closed"
    assert closed.expected == closed.missing == closed.observed == 0
    missing = candle_coverage([], Timeframe.H1, schedule(), FRIDAY, monday)
    assert missing.status == "missing"
    assert missing.expected == missing.missing == 24
    assert len(missing.missing_sample) == 10
    observations = [FRIDAY + timedelta(hours=i) for i in range(24)]
    complete = candle_coverage(observations, Timeframe.H1, schedule(), FRIDAY, monday)
    assert complete.status == "complete"
    assert complete.observed == complete.expected == 24


def test_holiday_partial_session_and_outside_schedule_observation():
    calendar = schedule(week={4: [{"start": 570, "end": 780}]})
    expected = [FRIDAY + timedelta(hours=i) for i in range(9, 13)]
    result = candle_coverage(expected, Timeframe.H1, calendar, FRIDAY, FRIDAY + timedelta(days=1))
    assert result.status == "complete"  # 09:30 shares the 09:00 bucket
    holiday = schedule(overrides={"2026-10-23": []})
    result = candle_coverage(expected, Timeframe.H1, holiday, FRIDAY, FRIDAY + timedelta(days=1))
    assert result.status == "schedule_mismatch"
    assert result.unexpected == 4
    assert result.missing == 0


@pytest.mark.parametrize(
    "start,hours", [(datetime(2026, 3, 8, tzinfo=UTC), 3), (datetime(2026, 11, 1, tzinfo=UTC), 5)]
)
def test_dst_open_bucket_counts(start, hours):
    calendar = TradingSchedule.model_validate(
        {"timezone": "America/New_York", "week": {6: [{"start": 0, "end": 240}]}}
    )
    coverage = candle_coverage([], Timeframe.H1, calendar, start, start + timedelta(days=1))
    assert coverage.expected == hours


def test_daily_bucket_with_partial_session():
    result = candle_coverage(
        [FRIDAY],
        Timeframe.D1,
        schedule(week={4: [{"start": 570, "end": 780}]}),
        FRIDAY,
        FRIDAY + timedelta(days=3),
    )
    assert result.expected == 1
    assert result.status == "complete"


@pytest.mark.parametrize(
    "change", [{"days": 0}, {"days": 31}, {"days": True}, {"instruments": []}, {"typo": 1}]
)
def test_manifest_rejects_invalid_options(change):
    with pytest.raises(ValueError):
        config(**change)


def test_manifest_requires_calendars_and_unique_normalized_pairs():
    assert config().instruments[0].pair == "AAPLX/USD"
    row = config().instruments[0].model_dump(mode="json")
    for invalid in (
        {**row, "timeframes": ["1h", "1h"]},
        {**row, "timeframes": ["2h"]},
        {**row, "trading_schedule": None},
    ):
        with pytest.raises(ValueError):
            config(instruments=[invalid])
    with pytest.raises(ValueError, match="duplicate pairs"):
        config(instruments=[row, {**row, "pair": "aaplx/usd"}])


@pytest.mark.parametrize(
    "start,end",
    [
        (FRIDAY, FRIDAY),
        (FRIDAY.replace(tzinfo=None), FRIDAY + timedelta(days=1)),
        (FRIDAY + timedelta(minutes=1), FRIDAY + timedelta(days=1)),
    ],
)
def test_invalid_coverage_ranges(start, end):
    with pytest.raises(ValueError):
        candle_coverage([], Timeframe.H1, schedule(), start, end)


async def test_refresh_isolates_timeframe_failure_and_applies_closing_margin(monkeypatch):
    from unittest.mock import AsyncMock

    import kaupo.data.xstocks_refresh as refresh

    manifest = config()
    manifest.instruments[0].timeframes = [Timeframe.H1, Timeframe.H4]
    ingest = AsyncMock(side_effect=[RuntimeError("hourly source failed"), 0])
    monkeypatch.setattr(refresh, "ingest_xstock_candles", ingest)
    monkeypatch.setattr(refresh, "get_candles", AsyncMock(return_value=[]))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    # A candle closing at midnight is not eligible one second after midnight.
    now = FRIDAY + timedelta(days=1, seconds=1)
    results = await refresh.refresh_xstocks(manifest, Session, object(), now=now)
    assert ingest.await_count == 2
    assert results[0].error == "RuntimeError: hourly source failed"
    assert results[1].error is None
    assert results[0].end == FRIDAY + timedelta(hours=23)
    assert results[1].end == FRIDAY + timedelta(hours=20)
    assert all(result.end - result.start == timedelta(days=3) for result in results)
    with pytest.raises(ValueError, match="timezone"):
        await refresh.refresh_xstocks(manifest, Session, object(), now=now.replace(tzinfo=None))
