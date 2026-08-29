"""Rolling-origin triage: verdict rules, window slicing, stitching, envelope, digest.

The build flow runs on a throwaway SQLite file with the backtest runners
faked (real backtests over canned candles are covered by the integration
tests on Postgres, which also exercise the reports upsert — the pg_insert
dialect does not compile on SQLite, so the unit tests build with
persist=False).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import kaupo.report.rolling as rolling
from kaupo.backtest.metrics import compute_metrics
from kaupo.data.assignments import create_assignment
from kaupo.db.models import Base, EquitySnapshotRow, FillRow, OrderRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Fill, OrderId, Pair, RunId, Side, Timeframe, new_id
from kaupo.report.rolling import (
    VERDICT_DIVERGES,
    VERDICT_ERROR,
    VERDICT_LAGS,
    VERDICT_LEADS,
    VERDICT_TRACKS,
    VERDICT_UNKNOWN,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)  # a Thursday, ISO week 2026-W35
TF = Timeframe.H1

HOLDER = """
from kaupo.sdk.protocol import StrategyBase

class Holder(StrategyBase):
    id = "holder"
    def on_candle(self, ctx):
        return []
"""

PORTER = """
from kaupo.sdk.protocol import PortfolioStrategyBase

class Porter(PortfolioStrategyBase):
    id = "porter"
    def on_candle(self, ctx):
        return []
"""


@pytest.fixture
async def sessionmaker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest.fixture
def strategies_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "strategies"
    directory.mkdir()
    (directory / "holder.py").write_text(HOLDER)
    (directory / "porter.py").write_text(PORTER)
    return directory


def _run_row(run_id: str, assignment_id: str, started: datetime, **config_extra: Any) -> RunRow:
    config: dict[str, Any] = {
        "assignment_id": assignment_id,
        "pair": "BTC/EUR",
        "timeframe": "1h",
        "starting_cash": 10_000.0,
        **config_extra,
    }
    return RunRow(
        id=run_id,
        mode="shadow",
        strategy_id="holder",
        strategy_version="v1",
        started_at=started,
        ended_at=started + timedelta(hours=1),
        status="completed",
        config=config,
    )


def _snapshot(run_id: str, ts: datetime, equity: float) -> EquitySnapshotRow:
    return EquitySnapshotRow(
        id=new_id(), run_id=run_id, ts=ts, equity=equity, cash=equity, unrealized_pnl=0.0
    )


def _fill(run_id: str, ts: datetime, side: str, price: float) -> tuple[OrderRow, FillRow]:
    order_id, fill_id = new_id(), new_id()
    order = OrderRow(
        id=order_id, run_id=run_id, ts=ts, pair="BTC/EUR", side=side, type="market", size=0.1, status="filled"
    )
    fill = FillRow(
        id=fill_id,
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


class TestVerdict:
    def test_no_activity_anywhere_tracks(self) -> None:
        verdict = rolling.verdict_for(
            backtest_sharpe=0.0, backtest_trips=0, shadow_sharpe=0.0, shadow_fills=0
        )
        assert verdict == VERDICT_TRACKS

    def test_equal_sharpes_track(self) -> None:
        verdict = rolling.verdict_for(
            backtest_sharpe=1.5, backtest_trips=3, shadow_sharpe=1.5, shadow_fills=6
        )
        assert verdict == VERDICT_TRACKS

    def test_tolerance_edges_track(self) -> None:
        for gap in (-rolling.SHARPE_TOLERANCE, rolling.SHARPE_TOLERANCE):
            verdict = rolling.verdict_for(
                backtest_sharpe=1.0, backtest_trips=3, shadow_sharpe=1.0 + gap, shadow_fills=6
            )
            assert verdict == VERDICT_TRACKS

    def test_beyond_tolerance_lags_and_leads(self) -> None:
        lag = rolling.verdict_for(backtest_sharpe=1.0, backtest_trips=3, shadow_sharpe=0.69, shadow_fills=6)
        lead = rolling.verdict_for(backtest_sharpe=1.0, backtest_trips=3, shadow_sharpe=1.31, shadow_fills=6)
        assert lag == VERDICT_LAGS
        assert lead == VERDICT_LEADS

    def test_exact_2x_count_ratio_is_not_divergence(self) -> None:
        # one round trip is two fills: an exact shadow of the backtest sits at 2.0
        verdict = rolling.verdict_for(
            backtest_sharpe=1.0, backtest_trips=3, shadow_sharpe=1.0, shadow_fills=6
        )
        assert verdict == VERDICT_TRACKS

    def test_count_divergence_beats_sharpe_match(self) -> None:
        verdict = rolling.verdict_for(
            backtest_sharpe=1.0, backtest_trips=2, shadow_sharpe=1.0, shadow_fills=5
        )
        assert verdict == VERDICT_DIVERGES

    def test_count_divergence_either_direction(self) -> None:
        verdict = rolling.verdict_for(
            backtest_sharpe=1.0, backtest_trips=4, shadow_sharpe=1.0, shadow_fills=1
        )
        assert verdict == VERDICT_DIVERGES

    def test_one_side_flat_falls_back_to_sharpe(self) -> None:
        # shadow did nothing while the backtest traded: not a count
        # divergence (needs both sides active), the sharpe gap decides
        verdict = rolling.verdict_for(
            backtest_sharpe=1.0, backtest_trips=3, shadow_sharpe=0.0, shadow_fills=0
        )
        assert verdict == VERDICT_LAGS


class TestWindowSlicing:
    def test_half_open_bounds(self) -> None:
        start, end = NOW - timedelta(days=7), NOW
        points = [
            (start - timedelta(hours=1), 1.0),  # before: out
            (start, 2.0),  # at start: in
            (start + timedelta(hours=1), 3.0),  # inside: in
            (end - timedelta(seconds=1), 4.0),  # just before end: in
            (end, 5.0),  # at end: out
            (end + timedelta(hours=1), 6.0),  # after: out
        ]
        assert rolling.slice_window(points, start, end) == points[1:4]


class TestCompareWindow:
    def test_no_chain_keeps_the_full_window(self) -> None:
        window_start = NOW - timedelta(days=30)
        assert rolling.compare_window(window_start, None) == window_start

    def test_older_chain_keeps_the_full_window(self) -> None:
        window_start = NOW - timedelta(days=30)
        assert rolling.compare_window(window_start, NOW - timedelta(days=45)) == window_start

    def test_younger_chain_aligns_to_its_start(self) -> None:
        window_start = NOW - timedelta(days=30)
        chain_start = NOW - timedelta(days=1, hours=7, minutes=12)
        assert rolling.compare_window(window_start, chain_start) == chain_start


class TestMinCoverage:
    def test_exact_floor_passes(self) -> None:
        # overlap exactly 7d, equity spanning exactly half of it
        assert rolling.has_min_coverage(overlap_days=7.0, span_days=3.5) is True

    def test_just_below_the_overlap_floor_fails(self) -> None:
        assert rolling.has_min_coverage(overlap_days=6.99, span_days=6.99) is False

    def test_just_below_the_span_half_fails(self) -> None:
        assert rolling.has_min_coverage(overlap_days=7.0, span_days=3.49) is False

    def test_full_coverage_passes(self) -> None:
        assert rolling.has_min_coverage(overlap_days=30.0, span_days=30.0) is True

    def test_dead_chain_fails_the_span_half(self) -> None:
        # an old chain whose equity stops a third into the window
        assert rolling.has_min_coverage(overlap_days=30.0, span_days=10.0) is False


class TestStitch:
    def test_rebase_onto_previous_run(self) -> None:
        groups = [
            [(NOW, 10_000.0), (NOW + timedelta(hours=1), 10_100.0)],
            [(NOW + timedelta(hours=2), 9_500.0), (NOW + timedelta(hours=3), 9_600.0)],
        ]
        stitched = rolling.stitch(groups)
        assert [v for _, v in stitched] == [10_000.0, 10_100.0, 10_100.0, 10_200.0]

    def test_overlap_later_run_wins(self) -> None:
        groups = [
            [(NOW + timedelta(hours=i), v) for i, v in enumerate((10_000.0, 10_010.0, 10_020.0, 10_030.0))],
            [(NOW + timedelta(hours=2 + i), v) for i, v in enumerate((500.0, 510.0))],
        ]
        stitched = rolling.stitch(groups)
        assert [ts for ts, _ in stitched] == [NOW + timedelta(hours=i) for i in (0, 1, 2, 3)]
        assert [v for _, v in stitched] == [10_000.0, 10_010.0, 10_010.0, 10_020.0]

    def test_empty_group_is_skipped(self) -> None:
        groups = [[(NOW, 10_000.0)], [], [(NOW + timedelta(hours=1), 10_050.0)]]
        # the run without snapshots contributes nothing; the next run still
        # rebases onto the last stitched point (offset -50)
        assert rolling.stitch(groups) == [(NOW, 10_000.0), (NOW + timedelta(hours=1), 10_000.0)]


class TestPeriodAndEnvelope:
    def test_iso_week(self) -> None:
        assert rolling.iso_week(NOW) == "2026-W35"
        assert rolling.iso_week(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-W01"
        assert rolling.iso_week(datetime(2025, 12, 29, tzinfo=UTC)) == "2026-W01"  # ISO year boundary

    def test_period_key_fits_the_column(self) -> None:
        key = rolling.period_key("2026-W35")
        assert key == "rolling-origin-2026-W35"
        assert len(key) <= 40  # reports.period is String(40) since migration 0012

    def test_envelope_shape(self) -> None:
        entries = [{"id": "a1", "verdict": "tracks"}]
        body = rolling.envelope("2026-W35", 30, NOW, entries)
        assert body == {
            "type": "rolling-origin",
            "period": "2026-W35",
            "window_days": 30,
            "generated_at": NOW.isoformat(),
            "assignments": entries,
        }


class TestDigestLines:
    def _body(self, *entries: dict[str, Any]) -> dict[str, Any]:
        return rolling.envelope("2026-W35", 30, NOW, list(entries))

    def test_full_entry(self) -> None:
        entry = {
            "id": "a1",
            "strategy_id": "holder",
            "pair": "BTC/EUR",
            "timeframe": "1h",
            "backtest": {"run_id": "r", "sharpe": 1.234, "num_round_trips": 3},
            "shadow": {"sharpe": 0.95, "num_fills": 6},
            "verdict": "lags",
        }
        (line,) = rolling.digest_lines(self._body(entry))
        assert line == "a1 holder BTC/EUR 1h: backtest sharpe 1.234 / shadow 0.95 (6 fills) — lags"

    def test_note_and_error_entries(self) -> None:
        note = {
            "id": "a2",
            "strategy_id": "holder",
            "pair": "BTC/EUR",
            "timeframe": "1h",
            "backtest": {"run_id": "r"},
            "shadow": {"note": "chain has 2.0 days so far"},
            "verdict": "unknown",
        }
        error = {
            "id": "a3",
            "strategy_id": "ghost",
            "pair": "SOL/EUR",
            "timeframe": "4h",
            "error": "unknown strategy 'ghost'",
            "verdict": "error",
        }
        lines = rolling.digest_lines(self._body(note, error))
        assert lines == [
            "a2 holder BTC/EUR 1h: chain has 2.0 days so far",
            "a3 ghost SOL/EUR 4h: error: unknown strategy 'ghost'",
        ]


FAKE_METRICS: dict[str, Any] = {
    "status": "completed",
    "sharpe": 1.0,
    "num_fills": 2,
    "num_round_trips": 1,
    "total_return_pct": 5.0,
}


def _fake_run_backtest(seen: list[Any], metrics: dict[str, Any] | Exception = FAKE_METRICS) -> Any:
    async def fake(request: Any, sm: Any) -> Any:
        seen.append(request)
        if isinstance(metrics, Exception):
            raise metrics
        return RunId(f"bt-{len(seen)}"), None, dict(metrics)

    return fake


async def _seed(session: AsyncSession, *rows: Any) -> None:
    for row in rows:
        session.add(row)
    await session.commit()


async def test_build_report_success_notes_and_exclusions(
    sessionmaker: async_sessionmaker[AsyncSession], strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = NOW - timedelta(days=7)
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="a1", strategy_id="holder", pair="BTC/EUR", timeframe="1h")
        await create_assignment(session, id="a2", strategy_id="ghost", pair="BTC/EUR", timeframe="1h")
        await create_assignment(session, id="a3", strategy_id="holder", pair="BTC/EUR", timeframe="1h")
        await create_assignment(session, id="a4", strategy_id="holder", pair="BTC/EUR", timeframe="1h")
        await create_assignment(
            session, id="a5", strategy_id="holder", pair="BTC/EUR", timeframe="1h", enabled=False
        )
        await create_assignment(
            session, id="a6", strategy_id="holder", pair="BTC/EUR", timeframe="1h", mode="live"
        )
        await create_assignment(
            session,
            id="a7",
            strategy_id="porter",
            pair="BTC/EUR",
            timeframe="1h",
            pairs=["BTC/EUR", "SOL/EUR"],
        )
        await session.commit()

    # a1's chain: two resume-linked runs whose snapshots continue at the same
    # level (a real resume chain's offset is zero), equity decaying in-window;
    # the snapshots span 4d of the 7d window so the coverage guard passes
    r1 = _run_row("run-1", "a1", start, chain_started_at=start.isoformat())
    r2 = _run_row(
        "run-2", "a1", start + timedelta(days=1), resumed_from="run-1", chain_started_at=start.isoformat()
    )
    snaps = [
        _snapshot("run-1", start + timedelta(hours=1), 10_000.0),
        _snapshot("run-2", start + timedelta(days=2), 10_000.0),
        _snapshot("run-2", start + timedelta(days=4), 9_900.0),
    ]
    buy_order, buy = _fill("run-1", start + timedelta(hours=1), "buy", 100.0)
    sell_order, sell = _fill("run-2", start + timedelta(days=2), "sell", 99.0)
    # a4's chain: one run, no snapshots yet (started two days ago)
    r4 = _run_row("run-4", "a4", NOW - timedelta(days=2))
    async with sm_scope(sessionmaker) as session:
        await _seed(session, r1, r2, r4, *snaps, buy_order, buy, sell_order, sell)

    seen: list[Any] = []
    portfolio_seen: list[Any] = []
    monkeypatch.setattr(rolling, "run_backtest", _fake_run_backtest(seen))
    monkeypatch.setattr(rolling, "run_portfolio_backtest", _fake_run_backtest(portfolio_seen))

    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=7, now=NOW, persist=False, strategies_dir=strategies_dir
    )

    assert body["type"] == "rolling-origin"
    assert body["period"] == "2026-W35"
    assert body["window_days"] == 7
    by_id = {entry["id"]: entry for entry in body["assignments"]}
    assert set(by_id) == {"a1", "a2", "a3", "a4", "a7"}  # disabled and live assignments are out

    # the runs row marker ties the backtest to the report, assignment, and slice
    assert seen[0].rolling_origin == {"period": "2026-W35", "assignment": "a1", "start": start.isoformat()}
    assert seen[0].start == start

    a1 = by_id["a1"]
    assert a1["start"] == start.isoformat()  # chain age == window: the full window is compared
    assert a1["backtest"]["run_id"] == "bt-1"
    assert a1["backtest"]["sharpe"] == 1.0
    expected_shadow = compute_metrics(
        equity=[
            (start + timedelta(hours=1), 10_000.0),
            (start + timedelta(days=2), 10_000.0),
            (start + timedelta(days=4), 9_900.0),
        ],
        fills=_expected_fills(start),
        timeframe=TF,
        starting_cash=10_000.0,
    )
    assert a1["shadow"] == expected_shadow
    assert a1["shadow"]["num_fills"] == 2
    assert a1["verdict"] == VERDICT_LAGS  # decaying shadow vs backtest sharpe 1.0

    assert by_id["a2"]["verdict"] == VERDICT_ERROR
    assert "unknown strategy" in by_id["a2"]["error"]
    assert by_id["a3"]["shadow"] == {"note": "no shadow runs yet"}
    assert by_id["a3"]["verdict"] == VERDICT_UNKNOWN
    assert by_id["a3"]["start"] == start.isoformat()  # no chain: the backtest keeps the full window
    assert by_id["a4"]["shadow"] == {"note": "chain has 2.0 days so far"}
    assert by_id["a4"]["verdict"] == VERDICT_UNKNOWN
    assert by_id["a4"]["backtest"] is None  # no equity in the window: the backtest is skipped too

    # portfolio assignment dispatched to the portfolio runner; no chain yet
    assert len(portfolio_seen) == 1
    assert [str(p) for p in portfolio_seen[0].pairs] == ["BTC/EUR", "SOL/EUR"]
    assert by_id["a7"]["shadow"] == {"note": "no shadow runs yet"}


def _expected_fills(start: datetime) -> list[Fill]:
    pair = Pair.parse("BTC/EUR")
    return [
        Fill(
            order_id=OrderId("o1"),
            pair=pair,
            side=Side.BUY,
            ts=start + timedelta(hours=1),
            price=100.0,
            size=0.1,
            fee=0.03,
        ),
        Fill(
            order_id=OrderId("o2"),
            pair=pair,
            side=Side.SELL,
            ts=start + timedelta(days=2),
            price=99.0,
            size=0.1,
            fee=0.03,
        ),
    ]


async def test_build_report_backtest_failure_isolated(
    sessionmaker: async_sessionmaker[AsyncSession], strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="a1", strategy_id="holder", pair="BTC/EUR", timeframe="1h")
        await session.commit()

    monkeypatch.setattr(
        rolling, "run_backtest", _fake_run_backtest([], ValueError("No kraken candles for BTC/EUR"))
    )
    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=7, now=NOW, persist=False, strategies_dir=strategies_dir
    )
    (entry,) = body["assignments"]
    assert entry["verdict"] == VERDICT_ERROR
    assert "No kraken candles" in entry["error"]
    assert entry["start"] == (NOW - timedelta(days=7)).isoformat()  # no chain: full window


async def test_build_report_young_chain_gets_no_verdict(
    sessionmaker: async_sessionmaker[AsyncSession], strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue #25 case: a profitable 30d backtest vs a 1.3d flat chain must
    not come out as "lags" — below the coverage floor the backtest is skipped
    before it runs (no runs row, backtest null, unknown with a note)."""
    chain_start = NOW - timedelta(days=1.3)
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="young", strategy_id="holder", pair="SOL/EUR", timeframe="4h")
        await session.commit()
    run = _run_row("run-y", "young", chain_start, chain_started_at=chain_start.isoformat())
    snaps = [
        _snapshot("run-y", chain_start + timedelta(hours=1), 10_000.0),
        _snapshot("run-y", chain_start + timedelta(hours=15), 10_000.0),
        _snapshot("run-y", NOW - timedelta(hours=1), 10_000.0),
    ]
    async with sm_scope(sessionmaker) as session:
        await _seed(session, run, *snaps)

    seen: list[Any] = []
    profitable = {"status": "completed", "sharpe": 7.454, "num_fills": 4, "num_round_trips": 2}
    monkeypatch.setattr(rolling, "run_backtest", _fake_run_backtest(seen, profitable))

    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=30, now=NOW, persist=False, strategies_dir=strategies_dir
    )

    (entry,) = body["assignments"]
    assert seen == []  # coverage decided before the backtest: never invoked
    assert entry["start"] == chain_start.isoformat()
    assert entry["backtest"] is None
    assert entry["shadow"]["sharpe"] == 0.0  # a correctly flat young chain
    assert entry["shadow"]["num_fills"] == 0
    assert entry["shadow"]["note"] == "chain covers 1.2d of the 30d window"
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert entry["verdict"] != VERDICT_LAGS


async def test_build_report_young_chain_daily_bars_no_error(
    sessionmaker: async_sessionmaker[AsyncSession], strategies_dir: Path
) -> None:
    """The production case from the issue #25 follow-up: a 1.2d chain at 1d
    bars has no closed candle inside its overlap — the report must skip the
    backtest and emit unknown with the coverage note, never an error.

    The backtest runner is NOT faked here: if the skip regresses, the real
    run_backtest dies on the empty candle table and the entry comes out as
    the error this test guards against.
    """
    chain_start = NOW - timedelta(days=1.2)
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="sol-1d", strategy_id="holder", pair="SOL/EUR", timeframe="1d")
        await session.commit()
    run = _run_row("run-d", "sol-1d", chain_start, timeframe="1d", chain_started_at=chain_start.isoformat())
    snaps = [
        _snapshot("run-d", chain_start + timedelta(hours=2), 10_000.0),
        _snapshot("run-d", chain_start + timedelta(hours=14), 10_000.0),
        _snapshot("run-d", NOW - timedelta(hours=1), 10_000.0),
    ]
    async with sm_scope(sessionmaker) as session:
        await _seed(session, run, *snaps)

    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=30, now=NOW, persist=False, strategies_dir=strategies_dir
    )

    (entry,) = body["assignments"]
    assert entry["backtest"] is None
    assert entry["shadow"]["note"] == "chain covers 1.1d of the 30d window"
    assert entry["verdict"] == VERDICT_UNKNOWN
    assert "error" not in entry
    async with sm_scope(sessionmaker) as session:
        rows = (await session.execute(select(RunRow))).scalars().all()
    assert [row.id for row in rows] == ["run-d"]  # no wasted backtest runs row


async def test_build_report_sub_candle_overlap_skips_backtest(
    sessionmaker: async_sessionmaker[AsyncSession], strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces below the floor: an overlap shorter than one full bar
    cannot hold a closed candle, so the backtest is skipped even when the
    (here lowered) coverage floor would pass."""
    monkeypatch.setattr(rolling, "MIN_OVERLAP_DAYS", 0.5)
    chain_start = NOW - timedelta(hours=12)  # 0.5d: passes the lowered floor, below one 1d bar
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="sol-1d", strategy_id="holder", pair="SOL/EUR", timeframe="1d")
        await session.commit()
    run = _run_row("run-d", "sol-1d", chain_start, timeframe="1d", chain_started_at=chain_start.isoformat())
    snaps = [
        _snapshot("run-d", chain_start + timedelta(hours=1), 10_000.0),
        _snapshot("run-d", chain_start + timedelta(hours=6), 10_000.0),
        _snapshot("run-d", NOW - timedelta(hours=1), 10_000.0),
    ]
    async with sm_scope(sessionmaker) as session:
        await _seed(session, run, *snaps)

    seen: list[Any] = []
    monkeypatch.setattr(rolling, "run_backtest", _fake_run_backtest(seen))

    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=30, now=NOW, persist=False, strategies_dir=strategies_dir
    )

    (entry,) = body["assignments"]
    assert seen == []  # the sub-bar guard fired before the backtest
    assert entry["backtest"] is None
    assert entry["shadow"]["note"] == "chain covers 0.4d of the 30d window"
    assert entry["verdict"] == VERDICT_UNKNOWN


async def test_build_report_lint_failure_degrades_all_entries(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "bad.py").write_text("import requests\n")
    async with sm_scope(sessionmaker) as session:
        await create_assignment(session, id="a1", strategy_id="holder", pair="BTC/EUR", timeframe="1h")
        await session.commit()

    body = await rolling.build_rolling_origin_report(
        sessionmaker, days=7, now=NOW, persist=False, strategies_dir=dirty
    )
    (entry,) = body["assignments"]
    assert entry["verdict"] == VERDICT_ERROR
    assert "strategies did not load" in entry["error"]


async def test_send_digest_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def capture(message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(rolling, "send_alert", capture)
    body = rolling.envelope(
        "2026-W35",
        30,
        NOW,
        [
            {
                "id": "a1",
                "strategy_id": "holder",
                "pair": "BTC/EUR",
                "timeframe": "1h",
                "backtest": {"run_id": "r", "sharpe": 1.0, "num_round_trips": 1},
                "shadow": {"sharpe": 0.9, "num_fills": 2},
                "verdict": "tracks",
            }
        ],
    )
    await rolling.send_digest(body)
    assert len(sent) == 1
    assert sent[0].startswith("Kaupo rolling-origin 2026-W35 (30d window):")
    assert "a1 holder BTC/EUR 1h: backtest sharpe 1.0 / shadow 0.9 (2 fills) — tracks" in sent[0]

    async def boom(message: str) -> None:
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(rolling, "send_alert", boom)
    await rolling.send_digest(body)  # an ntfy failure must not fail the report
