"""Calendar boundaries and watchdog delivery deadlines across market closures."""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from kaupo.api.schemas import AssignmentIn, AssignmentUpdate
from kaupo.core.supervisor import watchdog_is_stale
from kaupo.domain import Timeframe
from kaupo.market_calendar import TradingSchedule


def ts(value):
    return datetime.fromisoformat(value)


def schedule(**changes):
    return TradingSchedule.model_validate(
        {
            "timezone": "America/New_York",
            "week": {str(day): [{"start": 570, "end": 960}] for day in range(5)},
            **changes,
        }
    )


def stale(reference, now, timeframe=Timeframe.H1, calendar=None):
    return watchdog_is_stale(
        ts(reference), ts(now), timeframe, trading_schedule=calendar if calendar is not None else schedule()
    )


def test_weekend_and_monday_delivery_deadline():
    # Friday's final candle opens at 19:00 UTC and closes at 20:00 UTC.
    friday = "2026-10-23T19:00:00+00:00"
    assert not stale(friday, "2026-10-25T18:00:00+00:00")
    assert not stale(friday, "2026-10-26T14:09:59+00:00")
    assert stale(friday, "2026-10-26T14:10:00+00:00")
    # Receiving Monday's first candle restores a healthy watchdog.
    assert not stale("2026-10-26T13:00:00+00:00", "2026-10-26T14:10:00+00:00")


def test_missing_final_friday_candle_is_not_hidden_by_weekend():
    reference = "2026-10-23T18:00:00+00:00"
    assert not stale(reference, "2026-10-23T20:09:59+00:00")
    assert stale(reference, "2026-10-23T20:10:00+00:00")
    assert stale(reference, "2026-10-25T18:00:00+00:00")


def test_daily_candles_count_trading_dates_not_open_hours():
    reference = "2026-10-23T00:00:00+00:00"
    assert not stale(reference, "2026-10-26T23:59:59+00:00", Timeframe.D1)
    assert not stale(reference, "2026-10-27T00:09:59+00:00", Timeframe.D1)
    assert stale(reference, "2026-10-27T00:10:00+00:00", Timeframe.D1)


def test_holiday_and_early_close_override_week():
    calendar = schedule(overrides={"2026-10-23": [{"start": 570, "end": 780}], "2026-10-26": []})
    reference = "2026-10-23T16:00:00+00:00"
    assert not stale(reference, "2026-10-26T23:00:00+00:00", calendar=calendar)
    assert not stale(reference, "2026-10-27T14:09:59+00:00", calendar=calendar)
    assert stale(reference, "2026-10-27T14:10:00+00:00", calendar=calendar)


def test_weekend_open_override_and_intraday_break():
    calendar = schedule(overrides={"2026-10-24": [{"start": 600, "end": 660}]})
    assert stale("2026-10-23T19:00:00+00:00", "2026-10-24T15:10:00+00:00", calendar=calendar)
    split = schedule(week={0: [{"start": 540, "end": 720}, {"start": 780, "end": 1020}]})
    assert split.open_elapsed(ts("2026-10-26T13:00:00+00:00"), ts("2026-10-26T21:00:00+00:00")) == timedelta(
        hours=7
    )


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_full_local_day_spans_dst(day, hours):
    calendar = schedule(week={6: [{"start": 0, "end": 1440}]})
    beginning = ts(f"{day}T00:00:00-05:00") if hours == 23 else ts(f"{day}T00:00:00-04:00")
    assert calendar.open_elapsed(beginning, beginning + timedelta(hours=26)) == timedelta(hours=hours)


def test_dst_weekend_moves_monday_open_in_utc():
    reference = "2026-10-30T19:00:00+00:00"
    assert not stale(reference, "2026-11-02T15:09:59+00:00")
    assert stale(reference, "2026-11-02T15:10:00+00:00")


def test_ambiguous_and_missing_boundaries():
    # First 01:00 through last 01:30 includes both repeated wall times.
    calendar = schedule(week={6: [{"start": 60, "end": 90}]})
    assert calendar.open_elapsed(
        ts("2026-11-01T00:00:00+00:00"), ts("2026-11-02T00:00:00+00:00")
    ) == timedelta(minutes=90)
    # Missing 02:00 moves forward to 03:00 during the spring transition.
    calendar = schedule(week={6: [{"start": 120, "end": 240}]})
    assert calendar.open_elapsed(
        ts("2026-03-08T00:00:00+00:00"), ts("2026-03-09T00:00:00+00:00")
    ) == timedelta(hours=1)


def test_expanded_candles_are_counted_once():
    calendar = schedule(week={0: [{"start": 570, "end": 600}, {"start": 630, "end": 660}]})
    assert calendar.open_elapsed(
        ts("2026-10-26T00:00:00+00:00"), ts("2026-10-27T00:00:00+00:00"), candle_seconds=86400
    ) == timedelta(days=1)


def test_utc_daily_bucket_includes_session_on_adjacent_local_date():
    calendar = schedule(timezone="Asia/Tokyo", week={1: [{"start": 0, "end": 60}]})
    assert calendar.open_elapsed(
        ts("2026-10-26T00:00:00+00:00"), ts("2026-10-26T10:00:00+00:00"), candle_seconds=86400
    ) == timedelta(hours=10)


def test_empty_or_reversed_interval_and_naive_timestamps():
    calendar = schedule()
    start = ts("2026-10-26T13:00:00+00:00")
    assert calendar.open_elapsed(start, start) == timedelta(0)
    assert calendar.open_elapsed(start, start - timedelta(hours=1)) == timedelta(0)
    with pytest.raises(ValueError, match="timezone"):
        calendar.open_elapsed(start.replace(tzinfo=None), start)


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "invalid/zone"},
        {"timezone": "/etc/localtime"},
        {"week": {}},
        {"week": {7: [{"start": 0, "end": 60}]}},
        {"week": {0: [{"start": 0, "end": 1441}]}},
        {"week": {0: [{"start": 60, "end": 60}]}},
        {"week": {0: [{"start": 1000, "end": 600}]}},
        {"week": {0: [{"start": True, "end": 600}]}},
        {"week": {0: [{"start": 0, "end": 60}, {"start": 30, "end": 90}]}},
        {"overrides": {"not-a-date": []}},
        {"overrides": {"2026-10-26": [{"start": 0, "end": 60}, {"start": 30, "end": 90}]}},
        {"typo": "UTC"},
    ],
)
def test_invalid_calendar_rejected(changes):
    with pytest.raises(ValidationError):
        schedule(**changes)


def test_api_schema_and_json_roundtrip():
    calendar = schedule(overrides={"2026-10-26": []})
    payload = calendar.model_dump(mode="json")
    assert TradingSchedule.model_validate_json(calendar.model_dump_json()) == calendar
    request = AssignmentIn(strategy_id="s", pair="AAPLX/USD", timeframe="1h", trading_schedule=payload)
    assert request.trading_schedule == calendar
    assert AssignmentUpdate().model_fields_set == set()
    assert AssignmentUpdate(trading_schedule=None).model_fields_set == {"trading_schedule"}


def test_no_schedule_preserves_24_hour_watchdog():
    assert watchdog_is_stale(ts("2026-10-23T19:00:00+00:00"), ts("2026-10-25T18:00:00+00:00"), Timeframe.H1)
