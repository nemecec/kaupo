"""Rolling-origin triage end-to-end: real backtest vs a seeded resume chain.

One enabled shadow assignment, canned hourly candles, and a fake two-link
resume chain whose shadow reality is hand-computed. The report's backtest
must match a directly-invoked run_backtest, the shadow metrics the hand
computation, and the reports row must upsert idempotently on rerun.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.metrics import compute_metrics
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.assignments import create_assignment
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, ReportRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Fill, OrderId, Pair, Side, Timeframe, new_id
from kaupo.report.rolling import build_rolling_origin_report, iso_week, period_key
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)  # ISO week 2026-W35
DAYS = 7
BTC = Pair.parse("BTC/EUR")
TF = Timeframe.H1

BUYER = """
from pydantic import BaseModel

from kaupo.domain import OrderIntent, Side
from kaupo.sdk.protocol import StrategyBase

class BuyerParams(BaseModel):
    size: float = 0.1

class Buyer(StrategyBase):
    id = "buyer"
    params_schema = BuyerParams
    def on_candle(self, ctx):
        if ctx.position().size == 0:
            return [OrderIntent(pair=ctx.candle.pair, side=Side.BUY, size=self.params.size, reason="enter")]
        return []
"""


def candle(ts: datetime, price: float) -> Candle:
    return Candle(
        pair=BTC, timeframe=TF, ts=ts, open=price, high=price + 1, low=price - 1, close=price, volume=1.0
    )


def _run_row(run_id: str, started: datetime, **config_extra: object) -> RunRow:
    config = {
        "assignment_id": "a1",
        "pair": "BTC/EUR",
        "timeframe": "1h",
        "starting_cash": 10_000.0,
        **config_extra,
    }
    return RunRow(
        id=run_id,
        mode="shadow",
        strategy_id="buyer",
        strategy_version="v1",
        started_at=started,
        ended_at=started + timedelta(hours=6),
        status="completed",
        config=config,
    )


def _snapshot(run_id: str, ts: datetime, equity: float) -> EquitySnapshotRow:
    return EquitySnapshotRow(
        id=new_id(), run_id=run_id, ts=ts, equity=equity, cash=equity, unrealized_pnl=0.0
    )


def _fill(run_id: str, ts: datetime, side: str, price: float) -> tuple[OrderRow, FillRow]:
    order_id = new_id()
    order = OrderRow(
        id=order_id, run_id=run_id, ts=ts, pair="BTC/EUR", side=side, type="market", size=0.1, status="filled"
    )
    fill = FillRow(
        id=new_id(),
        order_id=order_id,
        run_id=run_id,
        ts=ts,
        pair="BTC/EUR",
        side=side,
        price=price,
        size=0.1,
        fee=0.03,
    )
    return order, fill


async def test_report_matches_backtest_and_shadow_reality(session: AsyncSession, tmp_path: Path) -> None:
    start = NOW - timedelta(days=DAYS)
    # rising zigzag: the buy-and-hold backtest earns a clearly positive sharpe
    candles = [
        candle(start + timedelta(hours=i), 100 + 0.3 * i + (2 if i % 2 else 0)) for i in range(DAYS * 24)
    ]
    await upsert_candles(session, candles)
    await create_assignment(session, id="a1", strategy_id="buyer", pair="BTC/EUR", timeframe="1h")

    # the fake chain: two resume-linked runs, equity decaying over the window
    chain_started = start.isoformat()
    r1 = _run_row("run-1", start, chain_started_at=chain_started)
    r2 = _run_row("run-2", start + timedelta(days=2), resumed_from="run-1", chain_started_at=chain_started)
    points = [
        (start + timedelta(hours=1), 10_100.0),
        (start + timedelta(hours=2), 9_800.0),
        (start + timedelta(days=3), 9_800.0),  # run-2 continues at run-1's level (offset 0)
        (start + timedelta(days=5), 9_600.0),
    ]
    snapshots = [
        _snapshot("run-1", *points[0]),
        _snapshot("run-1", *points[1]),
        _snapshot("run-2", *points[2]),
        _snapshot("run-2", *points[3]),
    ]
    buy_order, buy = _fill("run-1", start + timedelta(hours=1), "buy", 100.0)
    sell_order, sell = _fill("run-2", start + timedelta(days=3), "sell", 96.0)
    session.add_all([r1, r2])
    await session.flush()  # runs must exist before dependent rows (FK)
    session.add_all([*snapshots, buy_order, sell_order])
    await session.flush()  # orders must exist before their fills (FK)
    session.add_all([buy, sell])
    await session.commit()
    (tmp_path / "buyer.py").write_text(BUYER)

    body = await build_rolling_origin_report(get_sessionmaker(), days=DAYS, now=NOW, strategies_dir=tmp_path)

    # the envelope
    assert body["type"] == "rolling-origin"
    assert body["period"] == "2026-W35"
    assert body["window_days"] == DAYS
    (entry,) = body["assignments"]
    assert entry["id"] == "a1"
    assert entry["strategy_id"] == "buyer"
    assert entry["pair"] == "BTC/EUR"
    assert entry["timeframe"] == "1h"

    # the backtest side equals a directly-invoked run_backtest of the same config
    direct_id, _, direct_metrics = await run_backtest(
        BacktestRequest(
            strategy=load_strategies(tmp_path)["buyer"],
            params={},
            pair=BTC,
            timeframe=TF,
            start=start,
            end=NOW,
            starting_cash=10_000.0,
        ),
        get_sessionmaker(),
    )
    assert direct_id != entry["backtest"]["run_id"]  # separate runs rows
    assert {k: v for k, v in entry["backtest"].items() if k != "run_id"} == direct_metrics
    assert entry["backtest"]["num_round_trips"] == 1  # buy-and-hold, liquidated at the end

    # the shadow side equals the hand computation over the stitched chain
    expected_fills = [
        Fill(
            order_id=OrderId(buy_order.id),
            pair=BTC,
            side=Side.BUY,
            ts=start + timedelta(hours=1),
            price=100.0,
            size=0.1,
            fee=0.03,
        ),
        Fill(
            order_id=OrderId(sell_order.id),
            pair=BTC,
            side=Side.SELL,
            ts=start + timedelta(days=3),
            price=96.0,
            size=0.1,
            fee=0.03,
        ),
    ]
    assert entry["shadow"] == compute_metrics(
        equity=points, fills=expected_fills, timeframe=TF, starting_cash=10_000.0
    )
    assert entry["shadow"]["num_fills"] == 2
    assert entry["shadow"]["final_equity"] == 9_600.0

    # decaying shadow vs rising backtest: the shadow lags (the 2 fills vs 1
    # round trip sit exactly at the 2x count ratio, so no divergence)
    assert entry["shadow"]["sharpe"] < entry["backtest"]["sharpe"] - 0.3
    assert entry["verdict"] == "lags"

    # the runs row carries the audit marker
    marker_row = await session.get(RunRow, entry["backtest"]["run_id"])
    assert marker_row is not None
    assert marker_row.config["rolling_origin"] == {"period": "2026-W35", "assignment": "a1"}

    # persisted as one row per ISO week; a rerun in the same week replaces it
    key = period_key(iso_week(NOW))
    rows = (await session.execute(select(ReportRow).where(ReportRow.period == key))).scalars().all()
    assert len(rows) == 1
    assert rows[0].body == body
    first_row_id = rows[0].id

    rerun = await build_rolling_origin_report(get_sessionmaker(), days=DAYS, now=NOW, strategies_dir=tmp_path)

    def strip_run_ids(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # each build persists a fresh backtest runs row; everything else is stable
        return [{**e, "backtest": {k: v for k, v in e["backtest"].items() if k != "run_id"}} for e in entries]

    assert strip_run_ids(rerun["assignments"]) == strip_run_ids(body["assignments"])
    rows = (await session.execute(select(ReportRow).where(ReportRow.period == key))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_row_id  # same row, updated in place


async def test_report_without_chain_notes_the_new_assignment(session: AsyncSession, tmp_path: Path) -> None:
    start = NOW - timedelta(days=DAYS)
    candles = [candle(start + timedelta(hours=i), 100.0) for i in range(DAYS * 24)]
    await upsert_candles(session, candles)
    await create_assignment(session, id="fresh", strategy_id="buyer", pair="BTC/EUR", timeframe="1h")
    await session.commit()
    (tmp_path / "buyer.py").write_text(BUYER)

    body = await build_rolling_origin_report(get_sessionmaker(), days=DAYS, now=NOW, strategies_dir=tmp_path)

    (entry,) = body["assignments"]
    assert entry["shadow"] == {"note": "no shadow runs yet"}
    assert entry["verdict"] == "unknown"
    assert entry["backtest"]["status"] == "completed"  # the backtest side still ran
