"""Daily amounts retain their currency through aggregation and persistence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, ReportRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import new_id
from kaupo.report.daily import build_daily_report

pytestmark = pytest.mark.integration
DAY = datetime(2026, 10, 20, tzinfo=UTC)


async def add_run(session, config, pnl=10, active=True):
    run_id = new_id()
    session.add(
        RunRow(
            id=run_id,
            mode="shadow",
            strategy_id="test",
            strategy_version="1",
            started_at=DAY,
            status="running",
            config={"starting_cash": 1000, **config},
        )
    )
    await session.flush()
    if active:
        session.add(
            EquitySnapshotRow(
                id=new_id(),
                run_id=run_id,
                ts=DAY + timedelta(hours=1),
                equity=1000 + pnl,
                cash=1000 + pnl,
                unrealized_pnl=0,
            )
        )
    return run_id


@pytest.mark.parametrize(
    "second,expected,scalar,unknown",
    [
        ({"pair": "SOL/EUR"}, {"EUR": 30}, 30, 0),
        ({"pair": "SOL/USD"}, {"EUR": 10, "USD": 20}, None, 0),
        ({}, {"EUR": 10}, None, 1),
        ({"pairs": ["BTC/USD", "SOL/USD"], "pair": "BTC/USD,SOL/USD"}, {"EUR": 10, "USD": 20}, None, 0),
        ({"pair": "BTC/USD,SOL/USD"}, {"EUR": 10, "USD": 20}, None, 0),
        ({"pairs": ["BTC/USD", "SOL/EUR"]}, {"EUR": 10}, None, 1),
    ],
)
async def test_daily_totals_do_not_mix_quotes(session, second, expected, scalar, unknown):
    await add_run(session, {"pair": "BTC/EUR"}, pnl=10)
    await add_run(session, second, pnl=20)
    await add_run(session, {"pair": "SOL/GBP"}, active=False)
    await session.commit()
    body = await build_daily_report(get_sessionmaker(), DAY.date())
    totals = body["totals"]
    assert totals["total_pnl"] == scalar
    assert totals["quote_currency"] == ("EUR" if scalar is not None else None)
    assert totals["unclassified_runs"] == unknown
    assert {c: t["total_pnl"] for c, t in totals["by_currency"].items()} == expected
    assert totals["num_runs"] == 3
    assert totals["active_runs"] == 2
    assert next(r for r in body["runs"] if not r["active"])["quote_currency"] == "GBP"
    # Regeneration replaces the report and preserves denomination metadata.
    await build_daily_report(get_sessionmaker(), DAY.date())
    reports = (await session.execute(select(ReportRow))).scalars().all()
    assert len(reports) == 1
    assert reports[0].body["totals"] == totals


@pytest.mark.parametrize(
    "fee_currency,fee,expected,unknown",
    [
        (None, 0.26, {"EUR": 0.26}, 0),
        ("USD", 0.26, {"EUR": 0.0, "USD": 0.26}, 0),
        ("BTC", 0.00000123, {"BTC": 0.00000123, "EUR": 0.0}, 0),
        ("mixed", 0.26, {"EUR": 0.0}, 1),
    ],
)
async def test_fee_denominations_and_precision(session, fee_currency, fee, expected, unknown):
    run_id = await add_run(session, {"pair": "BTC/EUR"})
    oid = new_id()
    session.add(
        OrderRow(
            id=oid,
            run_id=run_id,
            ts=DAY,
            pair="BTC/EUR",
            side="buy",
            type="market",
            size=1,
            status="filled",
        )
    )
    await session.flush()
    session.add(
        FillRow(
            id=new_id(),
            order_id=oid,
            run_id=run_id,
            ts=DAY + timedelta(hours=1),
            pair="BTC/EUR",
            side="buy",
            price=100,
            size=1,
            fee=fee,
            fee_currency=fee_currency,
        )
    )
    await session.commit()
    body = await build_daily_report(get_sessionmaker(), DAY.date(), persist=False)
    totals = body["totals"]
    assert {c: t["total_fees"] for c, t in totals["by_currency"].items()} == expected
    assert totals["unclassified_runs"] == unknown
    assert totals["total_fills"] == 1
    assert totals["total_fees"] == (fee if fee_currency is None else None)
    assert body["runs"][0]["fees_paid"] == (fee if fee_currency is None else None)


async def test_empty_report_has_no_invented_currency(session):
    body = await build_daily_report(get_sessionmaker(), DAY.date(), persist=False)
    assert body["totals"]["by_currency"] == {}
    assert body["totals"]["quote_currency"] is None
    assert body["totals"]["total_pnl"] == 0
