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
from kaupo.config import default_maker_bps, default_taker_bps
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
        "fees": {"taker_bps": default_taker_bps(), "maker_bps": default_maker_bps()},
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
    points = [(start + timedelta(hours=i), 10_000.0 - i * 2) for i in range(DAYS * 24)]
    snapshots = [
        _snapshot("run-1" if ts < start + timedelta(days=2) else "run-2", ts, eq) for ts, eq in points
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
    assert entry["start"] == start.isoformat()  # chain as old as the window: the full window is compared
    assert entry["backtest"] is not None, entry

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
            # the report names its fees, so this side names them too: the
            # metrics below must match by construction, not because two
            # defaults happen to hold the same numbers (kaupo#44)
            taker_fee_bps=entry["fees"]["taker_bps"],
            maker_fee_bps=entry["fees"]["maker_bps"],
            liquidate_end=False,
        ),
        get_sessionmaker(),
    )
    assert direct_id != entry["backtest"]["run_id"]  # separate runs rows
    direct_metrics.pop("risk_rejections")  # only recorded for the full replay run
    assert {k: v for k, v in entry["backtest"].items() if k != "run_id"} == direct_metrics
    assert entry["backtest"]["num_round_trips"] == 0  # open positions stay open
    assert entry["backtest"]["num_fills"] == 1

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
    assert entry["shadow"]["final_equity"] == points[-1][1]

    # Decaying shadow vs rising backtest: the shadow lags on the common grid.
    assert entry["shadow"]["sharpe"] < entry["backtest"]["sharpe"] - 0.3
    assert entry["verdict"] == "lags"

    # the runs row carries the audit marker with the comparison slice used
    marker_row = await session.get(RunRow, entry["backtest"]["run_id"])
    assert marker_row is not None
    assert marker_row.config["rolling_origin"] == {
        "period": "2026-W35",
        "assignment": "a1",
        "start": start.isoformat(),
    }

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
    assert entry["shadow"] == {"reason_code": "no_shadow_runs", "note": "no shadow runs yet"}
    assert entry["verdict"] == "unknown"
    assert entry["backtest"]["status"] == "completed"  # the backtest side still ran
    assert entry["start"] == start.isoformat()  # no chain: the full window stays


async def test_young_chain_gets_unknown_not_lags(session: AsyncSession, tmp_path: Path) -> None:
    """The issue #25 case: a profitable 30d-window backtest vs a healthy 1.3d
    flat chain must not come out as "lags" — below the coverage floor the
    backtest is skipped before it runs (no runs row, no error)."""
    start = NOW - timedelta(days=DAYS)
    # rising zigzag over the whole window: a full-window backtest would be profitable
    candles = [
        candle(start + timedelta(hours=i), 100 + 0.3 * i + (2 if i % 2 else 0)) for i in range(DAYS * 24)
    ]
    await upsert_candles(session, candles)
    await create_assignment(session, id="young", strategy_id="buyer", pair="BTC/EUR", timeframe="1h")

    # a healthy 1.3-day-old chain: flat equity, no fills yet
    chain_start = NOW - timedelta(days=1.3)
    run = _run_row("run-y", chain_start, assignment_id="young", chain_started_at=chain_start.isoformat())
    snapshots = [
        _snapshot("run-y", chain_start + timedelta(hours=1), 10_000.0),
        _snapshot("run-y", chain_start + timedelta(hours=15), 10_000.0),
        _snapshot("run-y", NOW - timedelta(hours=1), 10_000.0),
    ]
    session.add(run)
    await session.flush()  # the run must exist before its snapshots (FK)
    session.add_all(snapshots)
    await session.commit()
    (tmp_path / "buyer.py").write_text(BUYER)

    body = await build_rolling_origin_report(get_sessionmaker(), days=DAYS, now=NOW, strategies_dir=tmp_path)

    (entry,) = body["assignments"]
    assert entry["start"] == chain_start.isoformat()
    assert entry["backtest"] is None  # coverage below the floor: the backtest never ran
    # the flat young chain shows sharpe 0.0 against what would be a profitable backtest...
    assert entry["shadow"]["sharpe"] == 0.0
    assert entry["shadow"]["num_fills"] == 0
    # ...but the coverage guard suppresses the bogus "lags" verdict
    assert entry["shadow"]["note"] == "chain covers 1.2d of the 7d window"
    assert entry["verdict"] == "unknown"
    assert "error" not in entry
    # no wasted backtest runs row: only the seeded chain run exists
    backtest_runs = (await session.execute(select(RunRow).where(RunRow.mode == "backtest"))).scalars().all()
    assert backtest_runs == []


@pytest.mark.parametrize("portfolio", [False, True], ids=["single-pair", "portfolio"])
async def test_replay_preserves_pre_window_entries_and_open_end_positions(
    session: AsyncSession, tmp_path: Path, portfolio: bool
) -> None:
    """A flat-start replay misses a pre-window edge; a full replay tracks it.

    The reference engine run supplies deterministic shadow evidence. One
    position closes inside the comparison window and another stays open at
    its end. Missing flat shadow bars must not change the aligned verdict.
    """
    from sqlalchemy import delete

    from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
    from kaupo.config import default_maker_bps, default_taker_bps
    from kaupo.db.models import CandleRow
    from kaupo.venues.paper import LIVE_MIRROR_MARKETABLE_LIMIT

    chain_start = NOW - timedelta(days=12)
    pairs = [BTC, Pair.parse("SOL/EUR")] if portfolio else [BTC]
    for pair in pairs:
        candles = []
        # First decision is the candle already open at the wall-clock
        # startup. Its cross-up must not be consumed as replay warm-up.
        for i in range(-1, 12 * 24):
            price = 99.0 if i < 0 or 8 * 24 <= i < 10 * 24 else 102.0
            if 2 * 24 <= i < 8 * 24:
                price = 120.0  # material pre-window gain; both baselines must match
            candles.append(
                Candle(
                    pair=pair,
                    timeframe=TF,
                    ts=chain_start + timedelta(hours=i),
                    open=price,
                    high=price + 1,
                    low=price - 1,
                    close=price,
                    volume=10,
                )
            )
        await upsert_candles(session, candles)
    base = "PortfolioStrategyBase" if portfolio else "StrategyBase"
    universe = "ctx.candles" if portfolio else "[ctx.candle.pair]"
    history = "ctx.history(pair, 2)" if portfolio else "ctx.history(2)"
    (tmp_path / "edge.py").write_text(
        f"from kaupo.sdk.protocol import {base}\n"
        "from kaupo.domain import OrderIntent, Side\n"
        f"class Edge({base}):\n"
        "    id = 'edge'\n"
        "    def on_candle(self, ctx):\n"
        "        orders = []\n"
        f"        for pair in {universe}:\n"
        f"            hist = {history}\n"
        "            if len(hist) < 2:\n"
        "                continue\n"
        "            if hist[0].close <= 100 < hist[1].close:\n"
        "                orders.append(OrderIntent(pair=pair, side=Side.BUY, size=1))\n"
        "            if hist[0].close >= 100 > hist[1].close:\n"
        "                orders.append(OrderIntent(pair=pair, side=Side.SELL, size=1))\n"
        "        return orders\n"
    )
    await create_assignment(
        session,
        id="edge-assignment",
        strategy_id="edge",
        pair=str(BTC),
        timeframe="1h",
        pairs=[str(pair) for pair in pairs] if portfolio else None,
    )
    await session.commit()
    strategy = load_strategies(tmp_path)["edge"]
    common = dict(
        strategy=strategy,
        params={},
        timeframe=TF,
        start=chain_start,
        end=NOW,
        liquidate_end=False,
        taker_fee_bps=default_taker_bps(),
        maker_fee_bps=default_maker_bps(),
        marketable_limit=LIVE_MIRROR_MARKETABLE_LIMIT,
    )
    if portfolio:
        shadow_id, _, _ = await run_portfolio_backtest(
            PortfolioBacktestRequest(pairs=pairs, **common), get_sessionmaker()
        )
    else:
        shadow_id, _, _ = await run_backtest(BacktestRequest(pair=BTC, **common), get_sessionmaker())
    run = await session.get(RunRow, shadow_id)
    assert run is not None
    run.mode = "shadow"
    wall_clock_start = chain_start + timedelta(minutes=15)
    run.started_at = wall_clock_start
    run.config = {
        **run.config,
        "assignment_id": "edge-assignment",
        "chain_started_at": wall_clock_start.isoformat(),
    }
    await session.execute(
        delete(EquitySnapshotRow).where(
            EquitySnapshotRow.run_id == shadow_id,
            EquitySnapshotRow.ts >= chain_start + timedelta(days=9),
            EquitySnapshotRow.ts < chain_start + timedelta(days=9, hours=8),
        )
    )
    await session.commit()

    body = await build_rolling_origin_report(get_sessionmaker(), days=7, now=NOW, strategies_dir=tmp_path)
    (entry,) = body["assignments"]
    assert entry["verdict"] == "tracks"
    assert entry["start"] == (NOW - timedelta(days=7)).isoformat()
    assert entry["replay_start"] == chain_start.isoformat()
    assert entry["liquidate_end"] is False
    assert entry["backtest"]["num_fills"] == entry["shadow"]["num_fills"] == 2 * len(pairs)
    assert entry["comparison"]["common_points"] == 7 * 24 - 8
    assert entry["shadow"]["starting_equity"] > 10_000.0
    for metric in ("starting_equity", "final_equity", "total_return_pct", "cagr_pct"):
        assert entry["shadow"][metric] == entry["backtest"][metric]
    assert entry["comparison"]["shadow_sharpe"] == entry["comparison"]["backtest_sharpe"]
    replay_run = await session.get(RunRow, entry["backtest"]["run_id"])
    assert replay_run is not None
    assert replay_run.config["start"] == chain_start.isoformat()
    assert replay_run.config["liquidate_end"] is False
    orders = (await session.scalars(select(OrderRow).where(OrderRow.run_id == replay_run.id))).all()
    assert all(order.reason != "end-of-run liquidation" for order in orders)
    assert sum(order.side == "buy" for order in orders) == 2 * len(pairs)

    # A partially retained archive cannot reconstruct the boundary state.
    await session.execute(delete(CandleRow).where(CandleRow.ts < NOW - timedelta(days=7)))
    await session.commit()
    body = await build_rolling_origin_report(get_sessionmaker(), days=7, now=NOW, strategies_dir=tmp_path)
    (entry,) = body["assignments"]
    assert entry["verdict"] == "unknown"
    assert entry["comparison"]["note"] == "replay history does not reach the chain origin"
    assert entry["verdict_reason"] == "incomplete_replay_history"
