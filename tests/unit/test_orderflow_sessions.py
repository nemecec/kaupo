"""Session meaning survives rollup, API conversion, and strategy access."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.text import Text
from typer.testing import CliRunner

from kaupo.api.deps import Principal
from kaupo.api.routes.data import orderflow_daily
from kaupo.cli.main import app
from kaupo.core.orderflow import StaticOrderFlowProvider
from kaupo.data.orderflow_daily import rollup_orderflow_daily
from kaupo.market_calendar import TradingSchedule

WEEKDAYS = TradingSchedule.model_validate(
    {
        "timezone": "America/New_York",
        "week": {day: [{"start": 570, "end": 960}] for day in range(5)},
        "overrides": {"2026-12-25": [], "2026-12-24": [{"start": 570, "end": 780}]},
    }
)


def empty_store():
    session = AsyncMock()
    session.scalar.return_value = None
    trades = MagicMock()
    trades.one.return_value = (0, 0, 0, 0, 0, 0)
    books = MagicMock()
    books.one.return_value = (0, None, None)
    session.execute.side_effect = [trades, books]
    return session


@pytest.mark.parametrize(
    ("day", "seconds", "state"),
    [
        (date(2026, 10, 23), 23400, "scheduled_open"),
        (date(2026, 10, 24), 0, "scheduled_closed"),
        (date(2026, 12, 25), 0, "scheduled_closed"),
        (date(2026, 12, 24), 12600, "scheduled_open"),
    ],
)
async def test_empty_rollup_distinguishes_sessions(day, seconds, state):
    row = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", day, WEEKDAYS)
    assert row.scheduled_open_seconds == seconds
    assert row.session_state == state
    assert row.trading_schedule == WEEKDAYS
    assert row.trade_count == row.book_snapshots == 0
    assert row.spread_mean_bps is None


async def test_no_schedule_means_unknown_even_with_no_observations():
    row = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", date(2026, 10, 24))
    assert row.session_state == "unknown"
    assert row.scheduled_open_seconds is None
    assert row.trading_schedule is None


@pytest.mark.parametrize(("day", "seconds"), [(date(2026, 3, 8), 3600), (date(2026, 11, 1), 10800)])
async def test_dst_session_uses_elapsed_seconds(day, seconds):
    schedule = TradingSchedule.model_validate(
        {"timezone": "America/New_York", "week": {6: [{"start": 60, "end": 180}]}}
    )
    row = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", day, schedule)
    assert row.scheduled_open_seconds == seconds


async def test_local_session_is_clipped_to_utc_day():
    schedule = TradingSchedule.model_validate(
        {"timezone": "America/New_York", "week": {4: [{"start": 1200, "end": 1440}]}}
    )
    friday = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", date(2026, 1, 2), schedule)
    saturday = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", date(2026, 1, 3), schedule)
    assert friday.session_state == "scheduled_closed"
    assert saturday.scheduled_open_seconds == 14400


async def test_observations_do_not_disappear_on_scheduled_closed_day():
    session = empty_store()
    trades, books = MagicMock(), MagicMock()
    trades.one.return_value = (1, 1, 0, 2, 0, 2)
    books.one.return_value = (1, 5, 5)
    session.execute.side_effect = [trades, books]
    row = await rollup_orderflow_daily(session, "kraken", "TEST/USD", date(2026, 10, 24), WEEKDAYS)
    assert row.session_state == "scheduled_closed"
    assert row.trade_count == row.book_snapshots == 1
    assert row.buy_volume == 2


async def test_api_and_static_provider_expose_persisted_session_without_early_visibility(monkeypatch):
    day = date(2026, 10, 24)
    row = await rollup_orderflow_daily(empty_store(), "kraken", "TEST/USD", day, WEEKDAYS)
    monkeypatch.setattr("kaupo.api.routes.data.get_orderflow_daily", AsyncMock(return_value=[row]))
    result = await orderflow_daily(
        Principal(admin=False),
        None,
        pair="TEST/USD",
        start=day,
        end=day + timedelta(days=1),
        limit=100,
        exchange="kraken",
    )
    payload = result[0].model_dump(mode="json")
    assert payload["session_state"] == "scheduled_closed"
    assert payload["scheduled_open_seconds"] == 0
    assert payload["trading_schedule"] == WEEKDAYS.model_dump(mode="json")
    provider = StaticOrderFlowProvider(daily={"TEST/USD": [row]})
    assert provider.tick_flow_daily("TEST/USD", 10, datetime(2026, 10, 24, 23, tzinfo=UTC)) == []
    assert provider.tick_flow_daily("TEST/USD", 10, datetime(2026, 10, 25, tzinfo=UTC)) == [row]
    assert replace(row, scheduled_open_seconds=None, trading_schedule=None).session_state == "unknown"


@pytest.mark.parametrize("pairs", [[], ["--pair", "BTC/EUR", "--pair", "ETH/EUR"]])
def test_cli_schedule_requires_one_explicit_pair(tmp_path, pairs):
    path = tmp_path / "schedule.json"
    path.write_text(WEEKDAYS.model_dump_json())
    result = CliRunner().invoke(app, ["ingest", "orderflow-rollup", "--trading-schedule", str(path), *pairs])
    assert result.exit_code == 2
    assert "requires exactly one --pair" in Text.from_ansi(result.output).plain


def test_cli_passes_schedule_and_empty_pair_to_rollup(tmp_path, monkeypatch):
    path = tmp_path / "schedule.json"
    path.write_text(WEEKDAYS.model_dump_json())
    session = AsyncMock()
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("kaupo.db.session.get_sessionmaker", lambda: maker)
    rollup = AsyncMock(
        return_value=MagicMock(
            pair="TEST/USD", trade_count=0, book_snapshots=0, session_state="scheduled_closed"
        )
    )
    upsert = AsyncMock()
    monkeypatch.setattr("kaupo.data.orderflow_daily.rollup_orderflow_daily", rollup)
    monkeypatch.setattr("kaupo.data.orderflow_daily.upsert_orderflow_daily", upsert)
    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "orderflow-rollup",
            "--pair",
            "TEST/USD",
            "--day",
            "2026-10-24",
            "--trading-schedule",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    rollup.assert_awaited_once_with(
        session, "kraken", "TEST/USD", date(2026, 10, 24), trading_schedule=WEEKDAYS
    )
    upsert.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert "scheduled_closed" in result.output
