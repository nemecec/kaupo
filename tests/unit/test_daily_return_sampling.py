"""Tests for UTC daily return sampling and schedule-aware metrics."""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from kaupo.backtest.metrics import compute_metrics
from kaupo.backtest.sampling import daily_equity
from kaupo.domain import Timeframe
from kaupo.market_calendar import TradingSchedule

NY_WEEKDAYS = TradingSchedule.model_validate(
    {"timezone": "America/New_York", "week": {day: [{"start": 570, "end": 960}] for day in range(5)}}
)
UTC_WEEKDAYS = TradingSchedule.model_validate(
    {"timezone": "UTC", "week": {day: [{"start": 0, "end": 1440}] for day in range(5)}}
)


def utc_day(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


def d1_curve(values: dict[int, float]) -> list[tuple[datetime, float]]:
    return [(utc_day(day), value) for day, value in values.items()]


def test_daily_equity_uses_delivered_closes_and_carries_only_closures() -> None:
    points, reason = daily_equity(
        d1_curve({22: 100, 23: 110, 26: 104.5, 27: 125.4}),
        Timeframe.D1,
        NY_WEEKDAYS,
        valuation_end=utc_day(28),
    )

    assert reason is None
    assert points == [
        (utc_day(23), 100),
        (utc_day(24), 110),
        (utc_day(25), 110),
        (utc_day(26), 110),
        (utc_day(27), 104.5),
        (utc_day(28), 125.4),
    ]


def test_daily_equity_excludes_terminal_incomplete_day_and_prevents_lookahead() -> None:
    points, reason = daily_equity(
        d1_curve({22: 100, 23: 110, 26: 104.5, 27: 125.4, 28: 999}),
        Timeframe.D1,
        NY_WEEKDAYS,
        valuation_end=utc_day(28),
    )

    assert reason is None
    assert points[-1] == (utc_day(28), 125.4)
    assert all(value != 999 for _, value in points)


def test_explicit_end_can_carry_final_value_through_closed_weekend() -> None:
    points, reason = daily_equity(
        d1_curve({22: 100, 23: 110}),
        Timeframe.D1,
        NY_WEEKDAYS,
        valuation_end=utc_day(26),
    )

    assert reason is None
    assert points == [(utc_day(23), 100), (utc_day(24), 110), (utc_day(25), 110), (utc_day(26), 110)]


def test_open_missing_day_is_not_treated_as_a_closure() -> None:
    points, reason = daily_equity(
        d1_curve({22: 100, 23: 110, 27: 125.4}),
        Timeframe.D1,
        NY_WEEKDAYS,
        valuation_end=utc_day(28),
    )

    assert points == []
    assert reason == "missing_scheduled_candles"


@pytest.mark.parametrize(
    ("equity", "end", "reason"),
    [
        ([(datetime(2026, 10, 22), 100)], None, "naive_equity_timestamp"),
        ([(utc_day(22), 0), (utc_day(23), 100)], None, "invalid_return_equity"),
        ([(utc_day(23), 100), (utc_day(22), 100)], None, "unordered_equity_timestamps"),
        (
            [(utc_day(22) + timedelta(hours=1), 100), (utc_day(23) + timedelta(hours=1), 100)],
            None,
            "unaligned_equity_timestamps",
        ),
        ([(utc_day(22), 100), (utc_day(23), 100)], datetime(2026, 10, 24), "naive_valuation_end"),
        ([], None, "insufficient_daily_returns"),
    ],
)
def test_daily_equity_rejects_invalid_inputs(equity, end, reason) -> None:
    assert daily_equity(equity, Timeframe.D1, NY_WEEKDAYS, end)[1] == reason


def test_holiday_override_is_a_valid_closure() -> None:
    schedule = NY_WEEKDAYS.model_copy(update={"overrides": {utc_day(26).date(): []}})
    points, reason = daily_equity(
        d1_curve({22: 100, 23: 110, 27: 104.5}), Timeframe.D1, schedule, valuation_end=utc_day(28)
    )

    assert reason is None
    assert points[-2:] == [(utc_day(27), 110), (utc_day(28), 104.5)]


def test_dst_transition_uses_local_schedule_window() -> None:
    schedule = TradingSchedule.model_validate(
        {"timezone": "America/New_York", "week": {6: [{"start": 60, "end": 180}]}}
    )
    # The Sunday session is 06:00 to 07:00 UTC across the spring-forward gap.
    assert schedule.open_elapsed(
        datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 8, 8, tzinfo=UTC)
    ) == timedelta(hours=1)


def test_metrics_use_daily_returns_and_hand_checked_ratios() -> None:
    curve = d1_curve({22: 100, 23: 110, 26: 104.5, 27: 125.4})
    metrics = compute_metrics(
        curve, [], Timeframe.D1, 100, trading_schedule=NY_WEEKDAYS, valuation_end=utc_day(28)
    )
    # Saturday and Sunday are explicit zero-return closure days.
    returns = np.array([0.1, 0.0, 0.0, -0.05, 0.2])
    expected_sharpe = returns.mean() / returns.std(ddof=1) * math.sqrt(365.25)
    assert metrics["return_sampling"]["num_returns"] == 5
    assert metrics["sharpe"] == round(expected_sharpe, 3)
    # A single negative observation has zero sample downside deviation.
    assert metrics["sortino"] == 0.0
    assert metrics["return_sampling"]["start"] == utc_day(23).isoformat()

    extended = [*curve, (utc_day(28), 112.86)]
    metrics = compute_metrics(
        extended, [], Timeframe.D1, 100, trading_schedule=NY_WEEKDAYS, valuation_end=utc_day(29)
    )
    returns = np.array([0.1, 0.0, 0.0, -0.05, 0.2, -0.1])
    expected_sortino = returns.mean() / np.array([-0.05, -0.1]).std(ddof=1) * math.sqrt(365.25)
    assert metrics["sortino"] == round(expected_sortino, 3)


def test_metrics_return_none_for_insufficient_or_invalid_samples() -> None:
    metrics = compute_metrics(
        d1_curve({22: 100, 23: 110}),
        [],
        Timeframe.D1,
        100,
        trading_schedule=NY_WEEKDAYS,
        valuation_end=utc_day(24),
    )
    assert metrics["sharpe"] is None
    assert metrics["sortino"] is None
    assert metrics["return_sampling"]["reason_code"] == "insufficient_daily_returns"

    invalid = compute_metrics(
        d1_curve({22: 100, 23: 110, 27: 125}),
        [],
        Timeframe.D1,
        100,
        trading_schedule=NY_WEEKDAYS,
        valuation_end=utc_day(28),
    )
    assert invalid["sharpe"] is None
    assert invalid["sortino"] is None
    assert invalid["return_sampling"]["reason_code"] == "missing_scheduled_candles"


def test_legacy_metrics_without_schedule_are_unchanged() -> None:
    metrics = compute_metrics(
        [(utc_day(22) + timedelta(hours=i), value) for i, value in enumerate([100, 110, 104.5, 125.4])],
        [],
        Timeframe.H1,
        100,
    )
    returns = np.array([0.1, -0.05, 0.2])
    expected = returns.mean() / returns.std(ddof=1) * math.sqrt(Timeframe.H1.periods_per_year)
    assert metrics["sharpe"] == round(expected, 3)
    assert "return_sampling" not in metrics


def test_h1_and_d1_sampling_have_the_same_daily_end_snapshots() -> None:
    values = {19: 100.0, 20: 110.0, 21: 104.5, 22: 125.4, 23: 118.0, 26: 130.0, 27: 120.0}
    daily = daily_equity(d1_curve(values), Timeframe.D1, UTC_WEEKDAYS, valuation_end=utc_day(28))
    hourly: list[tuple[datetime, float]] = []
    cursor = utc_day(19)
    while cursor < utc_day(28):
        if cursor.weekday() < 5:
            hourly.append((cursor, values[cursor.day]))
        cursor += timedelta(hours=1)
    hourly_sample = daily_equity(hourly, Timeframe.H1, UTC_WEEKDAYS, valuation_end=utc_day(28))

    assert daily[1] is None
    assert hourly_sample[1] is None
    assert hourly_sample[0] == daily[0]


def test_rolling_comparison_accepts_complete_week_with_closed_weekend() -> None:
    from kaupo.report.rolling import aligned_comparison

    start, end = utc_day(19), utc_day(26)
    curve = [
        (utc_day(day) + timedelta(hours=hour), 100 + day + hour / 100)
        for day in range(19, 24)
        for hour in range(13, 20)
    ]
    result = aligned_comparison(
        curve, curve, timeframe=Timeframe.H1, start=start, end=end, trading_schedule=NY_WEEKDAYS
    )
    assert "note" not in result
    assert result["shadow_sharpe"] == result["backtest_sharpe"]
    assert result["return_sampling"]["num_returns"] == 6

    broken = curve[:10] + curve[11:]
    result = aligned_comparison(
        broken, broken, timeframe=Timeframe.H1, start=start, end=end, trading_schedule=NY_WEEKDAYS
    )
    assert result["reason_code"] == "missing_scheduled_candles"
