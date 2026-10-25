"""PostgreSQL schedule evidence survives daily report persistence and replacement."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kaupo.data.assignments import create_assignment
from kaupo.db.models import EquitySnapshotRow, ReportRow, RunAssignmentRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.market_calendar import TradingSchedule
from kaupo.report.daily import build_daily_report

pytestmark = pytest.mark.integration


async def test_daily_closure_report_persists_and_rechecks_evidence(session):
    saturday = datetime(2026, 10, 24, tzinfo=UTC)
    friday = saturday - timedelta(hours=5)
    created = saturday - timedelta(days=3)
    schedule = TradingSchedule.model_validate(
        {
            "timezone": "America/New_York",
            "week": {day: [{"start": 570, "end": 960}] for day in range(5)},
        }
    )
    await create_assignment(
        session, id="calendar", strategy_id="s", pair="BTC/EUR", timeframe="1h", trading_schedule=schedule
    )
    assignment = await session.get(RunAssignmentRow, "calendar")
    assignment.created_at = assignment.updated_at = created
    for run_id in ("closed", "stale"):
        session.add(
            RunRow(
                id=run_id,
                mode="shadow",
                strategy_id="s",
                strategy_version="v",
                started_at=created,
                status="running",
                config={"assignment_id": "calendar", "pair": "BTC/EUR", "timeframe": "1h"},
            )
        )
    await session.flush()
    for run_id, when in (("closed", friday), ("stale", friday - timedelta(hours=1))):
        session.add(
            EquitySnapshotRow(
                id=f"snapshot-{run_id}", run_id=run_id, ts=when, equity=1000, cash=1000, unrealized_pnl=0
            )
        )
    await session.commit()

    report = await build_daily_report(get_sessionmaker(), saturday.date())
    assert report["totals"]["market_closed_runs"] == 1
    assert report["totals"]["inactive_runs"] == 1
    persisted = (await session.execute(select(ReportRow))).scalar_one()
    assert persisted.body == report
    report_id = persisted.id
    assignment.updated_at = saturday + timedelta(days=1)
    await session.commit()

    rerun = await build_daily_report(get_sessionmaker(), saturday.date())
    assert rerun["totals"]["market_closed_runs"] == 0
    assert rerun["totals"]["inactive_runs"] == 2
    await session.refresh(persisted)
    assert persisted.id == report_id
    assert persisted.body == rerun
