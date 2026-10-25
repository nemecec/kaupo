"""Scheduled closures remain distinct from missing observations and stale runs."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kaupo.data.assignments import Assignment, create_assignment
from kaupo.db.models import Base, EquitySnapshotRow, RunAssignmentRow, RunRow
from kaupo.market_calendar import TradingSchedule
from kaupo.report.daily import _inactivity_reason, build_daily_report

SATURDAY = datetime(2026, 10, 24, tzinfo=UTC)
FRIDAY = SATURDAY - timedelta(hours=5)  # final NY session candle: 19:00-20:00 UTC
CREATED = SATURDAY - timedelta(days=3)
SCHEDULE = TradingSchedule.model_validate(
    {"timezone": "America/New_York", "week": {day: [{"start": 570, "end": 960}] for day in range(5)}}
)


def assignment(**changes):
    return replace(
        Assignment(
            id="a",
            strategy_id="s",
            pair="BTC/EUR",
            pairs=None,
            timeframe="1h",
            mode="shadow",
            params={},
            enabled=True,
            starting_cash=1000,
            created_at=CREATED,
            updated_at=CREATED,
            trading_schedule=SCHEDULE,
        ),
        **changes,
    )


def run(**changes):
    return RunRow(
        **{
            "id": "run",
            "mode": "shadow",
            "strategy_id": "s",
            "strategy_version": "v",
            "started_at": CREATED,
            "status": "running",
            "config": {
                "assignment_id": "a",
                "pair": "BTC/EUR",
                "timeframe": "1h",
                "starting_cash": 1000,
            },
            **changes,
        }
    )


def snapshot(when=FRIDAY, run_id="run"):
    return EquitySnapshotRow(
        id=f"snapshot-{run_id}-{when.isoformat()}",
        run_id=run_id,
        ts=when,
        equity=1005,
        cash=1005,
        unrealized_pnl=0,
    )


def reason(row=None, a=None, prev=None, day=SATURDAY):
    return _inactivity_reason(
        row or run(), a or assignment(), prev or snapshot(), day, day + timedelta(days=1)
    )


def test_healthy_weekend_is_market_closed():
    assert reason() == "scheduled_closure"
    assert reason(day=SATURDAY + timedelta(days=1)) == "scheduled_closure"


def test_missing_friday_candle_is_not_excused():
    assert reason(prev=snapshot(FRIDAY - timedelta(hours=1))) == "stale_before_closure"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"trading_schedule": None}, "no_schedule_evidence"),
        ({"enabled": False}, "assignment_mismatch"),
        ({"pair": "BTC/USD"}, "assignment_mismatch"),
        ({"mode": "live"}, "assignment_mismatch"),
        ({"strategy_id": "different"}, "assignment_mismatch"),
        ({"timeframe": "4h"}, "assignment_mismatch"),
        ({"updated_at": FRIDAY + timedelta(minutes=1)}, "assignment_changed_since_evidence"),
        ({"updated_at": SATURDAY + timedelta(days=3)}, "assignment_changed_since_evidence"),
    ],
)
def test_unknown_or_changed_schedule_does_not_rewrite_history(changes, expected):
    assert reason(a=assignment(**changes)) == expected


def test_missing_assignment_or_snapshot_is_not_claimed_closed():
    end = SATURDAY + timedelta(days=1)
    assert _inactivity_reason(run(), None, snapshot(), SATURDAY, end) == "no_schedule_evidence"
    assert _inactivity_reason(run(), assignment(), None, SATURDAY, end) == "no_prior_snapshot"


@pytest.mark.parametrize("changes", [{"status": "halted"}, {"ended_at": SATURDAY + timedelta(hours=2)}])
def test_stopped_runs_are_not_claimed_closed(changes):
    assert reason(row=run(**changes)) == "run_stopped"


def test_run_that_stopped_after_period_can_explain_historical_closure():
    assert reason(row=run(status="halted", ended_at=SATURDAY + timedelta(days=3))) == "scheduled_closure"


def test_open_day_and_early_close_are_not_whole_day_closures():
    monday = SATURDAY + timedelta(days=2)
    assert reason(day=monday) == "market_was_open"
    early_close = TradingSchedule.model_validate(
        {**SCHEDULE.model_dump(mode="json"), "overrides": {monday.date(): [{"start": 570, "end": 780}]}}
    )
    assert reason(day=monday, a=assignment(trading_schedule=early_close)) == "market_was_open"


def test_holiday_override_is_closed():
    monday = SATURDAY + timedelta(days=2)
    holiday = SCHEDULE.model_copy(update={"overrides": {monday.date(): []}})
    assert reason(day=monday, a=assignment(trading_schedule=holiday)) == "scheduled_closure"


def test_daily_candles_and_utc_date_overlap():
    daily = assignment(timeframe="1d")
    row = run(config={"pair": "BTC/EUR", "timeframe": "1d"})
    friday_daily = snapshot(SATURDAY - timedelta(days=1))
    assert reason(row=row, a=daily, prev=friday_daily) == "scheduled_closure"
    assert reason(row=row, a=daily, prev=snapshot(SATURDAY - timedelta(days=2))) == "stale_before_closure"
    # A Sunday local session crosses into Monday in UTC.
    pacific = TradingSchedule.model_validate(
        {"timezone": "America/Los_Angeles", "week": {6: [{"start": 1200, "end": 1440}]}}
    )
    assert (
        reason(day=SATURDAY + timedelta(days=2), a=assignment(trading_schedule=pacific)) == "market_was_open"
    )


def test_naive_database_timestamps_are_utc():
    assert (
        reason(
            a=assignment(updated_at=CREATED.replace(tzinfo=None)),
            prev=snapshot(FRIDAY.replace(tzinfo=None)),
        )
        == "scheduled_closure"
    )


def test_invalid_legacy_timeframe_is_not_claimed_closed():
    assert (
        reason(row=run(config={"pair": "BTC/EUR", "timeframe": "bad"}), a=assignment(timeframe="bad"))
        == "invalid_timeframe"
    )


@pytest.fixture
async def sm(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/daily.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_report_joins_assignments_and_keeps_money_observation_based(sm):
    async with sm() as session:
        await create_assignment(
            session, id="a", strategy_id="s", pair="BTC/EUR", timeframe="1h", trading_schedule=SCHEDULE
        )
        stored = await session.get(RunAssignmentRow, "a")
        stored.created_at = stored.updated_at = CREATED
        session.add_all(
            [
                run(id="closed"),
                run(id="stale"),
                run(id="active"),
                run(id="legacy", config={"pair": "BTC/EUR"}),
            ]
        )
        await session.flush()
        session.add_all(
            [
                snapshot(run_id="closed"),
                snapshot(FRIDAY - timedelta(hours=1), "stale"),
                snapshot(SATURDAY + timedelta(hours=2), "active"),
            ]
        )
        await session.commit()
    body = await build_daily_report(sm, SATURDAY.date(), persist=False)
    by_id = {r["run_id"]: r for r in body["runs"]}
    assert by_id["closed"]["activity_state"] == "market_closed"
    assert by_id["closed"]["active"] is False
    assert by_id["closed"]["last_snapshot_at"] == FRIDAY.isoformat()
    assert "pnl" not in by_id["closed"]  # no new valuation during the closure
    assert by_id["stale"]["activity_reason"] == "stale_before_closure"
    assert by_id["legacy"]["activity_state"] == "inactive"
    assert by_id["legacy"]["last_snapshot_at"] is None
    assert by_id["active"]["activity_state"] == "active"  # observations take priority over calendar
    assert body["totals"]["active_runs"] == 1
    assert body["totals"]["market_closed_runs"] == 1
    assert body["totals"]["inactive_runs"] == 2
    assert body["totals"]["total_pnl"] == 5
    assert body["totals"]["by_currency"]["EUR"]["active_runs"] == 1
    # A later edit cannot retrospectively declare the old silent day closed.
    async with sm() as session:
        stored = await session.get(RunAssignmentRow, "a")
        stored.updated_at = SATURDAY + timedelta(days=2)
        await session.commit()
    rerun = await build_daily_report(sm, SATURDAY.date(), persist=False)
    assert rerun["totals"]["market_closed_runs"] == 0
    assert rerun["totals"]["inactive_runs"] == 3


async def test_empty_report_activity_counts(sm):
    body = await build_daily_report(sm, SATURDAY.date(), persist=False)
    assert body["totals"]["active_runs"] == 0
    assert body["totals"]["market_closed_runs"] == 0
    assert body["totals"]["inactive_runs"] == 0
