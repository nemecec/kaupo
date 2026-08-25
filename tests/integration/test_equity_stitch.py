"""Account-level stitched equity: service math and endpoint contract."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import get_settings
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import dispose_engine
from kaupo.domain import new_id
from kaupo.report.equity import stitch_equity

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    # auth disabled for these tests (restore afterwards)
    saved = {k: os.environ.get(k) for k in ("KAUPO_ADMIN_TOKEN", "KAUPO_READONLY_TOKEN")}
    os.environ.pop("KAUPO_ADMIN_TOKEN", None)
    os.environ.pop("KAUPO_READONLY_TOKEN", None)
    get_settings.cache_clear()
    await dispose_engine()

    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _mk_run(
    run_id: str,
    mode: str,
    strategy_id: str,
    started_at: datetime,
    status: str = "completed",
) -> RunRow:
    return RunRow(
        id=run_id,
        mode=mode,
        strategy_id=strategy_id,
        strategy_version="v1",
        started_at=started_at,
        ended_at=started_at + timedelta(hours=1) if status != "running" else None,
        status=status,
        config={"pair": "BTC/EUR", "timeframe": "1h"},
    )


async def _runs(session: AsyncSession, *rows: RunRow) -> None:
    session.add_all(rows)
    await session.flush()  # runs must exist before dependent snapshots (FK)


def _snaps(
    session: AsyncSession,
    run_id: str,
    equities: list[float],
    start: datetime,
    step: timedelta = timedelta(hours=1),
) -> None:
    for i, equity in enumerate(equities):
        session.add(
            EquitySnapshotRow(
                id=new_id(),
                run_id=run_id,
                ts=start + i * step,
                equity=equity,
                cash=equity,
                unrealized_pnl=0.0,
            )
        )


async def test_stitch_two_sequential_runs_is_continuous(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=3)),
    )
    _snaps(session, "run-a", [10_000, 10_050, 10_100], BASE)
    # fresh ledger: run-b restarts at 10_000
    _snaps(session, "run-b", [10_000, 10_025, 10_050], BASE + timedelta(hours=3))
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    assert [p.ts for p in points] == [BASE + timedelta(hours=i) for i in range(6)]
    assert [p.equity for p in points] == [10_000, 10_050, 10_100, 10_100, 10_125, 10_150]
    # continuity at the boundary: run-b's first stitched == run-a's last stitched
    assert points[3].equity == points[2].equity


async def test_stitch_chains_three_runs(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=2)),
        _mk_run("run-c", "shadow", "s", BASE + timedelta(hours=4), status="running"),
    )
    _snaps(session, "run-a", [10_000, 10_100], BASE)
    _snaps(session, "run-b", [9_500, 9_600], BASE + timedelta(hours=2))
    _snaps(session, "run-c", [10_000, 9_900], BASE + timedelta(hours=4))
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    # b rebased onto a's end (+600), c rebased onto b's stitched end (10_200)
    assert [p.equity for p in points] == [10_000, 10_100, 10_100, 10_200, 10_200, 10_100]


async def test_stitch_skips_runs_without_snapshots(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-empty", "shadow", "s", BASE + timedelta(hours=2)),  # no snapshots
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=4)),
    )
    _snaps(session, "run-a", [10_000, 10_100], BASE)
    _snaps(session, "run-b", [10_000, 10_050], BASE + timedelta(hours=4))
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    # identical to run-a and run-b stitched directly
    assert [p.equity for p in points] == [10_000, 10_100, 10_100, 10_150]


async def test_stitch_overlap_later_run_wins(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=2)),
    )
    _snaps(session, "run-a", [10_000, 10_010, 10_020, 10_030], BASE)
    # starts at ts=BASE+2h: run-a's points at +2h and +3h are superseded
    _snaps(session, "run-b", [500, 510, 520], BASE + timedelta(hours=2))
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    assert [p.ts for p in points] == [BASE + timedelta(hours=i) for i in (0, 1, 2, 3, 4)]
    # offset = run-a's last kept stitched (10_010) - run-b's first snapshot (500)
    assert [p.equity for p in points] == [10_000, 10_010, 10_010, 10_020, 10_030]


async def test_stitch_offset_keeps_cash_consistent(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=2)),
    )
    _snaps(session, "run-a", [10_000, 10_100], BASE)
    session.add(
        EquitySnapshotRow(
            id=new_id(),
            run_id="run-b",
            ts=BASE + timedelta(hours=2),
            equity=10_000,
            cash=8_000,
            unrealized_pnl=2_000,
        )
    )
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    last = points[-1]
    assert last.equity == 10_100
    assert last.cash == 8_100  # same offset as equity
    assert last.unrealized_pnl == 2_000
    assert last.equity - last.cash == last.unrealized_pnl


async def test_stitch_scoped_to_mode_and_strategy(session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "backtest", "s", BASE + timedelta(hours=1)),  # same strategy, other mode
        _mk_run("run-c", "shadow", "other", BASE + timedelta(hours=2)),  # other strategy
    )
    _snaps(session, "run-a", [10_000], BASE)
    _snaps(session, "run-b", [50_000], BASE + timedelta(hours=1))
    _snaps(session, "run-c", [99_000], BASE + timedelta(hours=2))
    await session.commit()

    points = await stitch_equity(session, "shadow", "s")

    assert [p.equity for p in points] == [10_000]


async def test_account_equity_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    await _runs(
        session,
        _mk_run("run-a", "shadow", "s", BASE),
        _mk_run("run-b", "shadow", "s", BASE + timedelta(hours=2), status="running"),
    )
    _snaps(session, "run-a", [10_000, 10_100], BASE)
    _snaps(session, "run-b", [10_000, 10_050], BASE + timedelta(hours=2))
    await session.commit()

    r = await client.get("/api/v1/equity/account", params={"strategy": "s"})  # mode defaults to shadow
    assert r.status_code == 200
    body = r.json()
    assert [p["equity"] for p in body] == [10_000.0, 10_100.0, 10_100.0, 10_150.0]
    assert body[0]["ts"].startswith("2026-01-01T00:00:00")  # ISO 8601
    assert [p["ts"] for p in body] == sorted(p["ts"] for p in body)  # ascending
    assert set(body[0]) == {"ts", "equity", "cash", "unrealized_pnl"}

    # a strategy that only has runs in another mode: known, but no shadow history
    await _runs(session, _mk_run("run-c", "backtest", "bt-only", BASE))
    _snaps(session, "run-c", [5_000], BASE)
    await session.commit()
    r = await client.get("/api/v1/equity/account", params={"strategy": "bt-only", "mode": "shadow"})
    assert r.status_code == 200
    assert r.json() == []

    r = await client.get("/api/v1/equity/account", params={"strategy": "bt-only", "mode": "backtest"})
    assert r.status_code == 200
    assert [p["equity"] for p in r.json()] == [5_000.0]


async def test_account_equity_unknown_strategy_is_404(client: AsyncClient, session: AsyncSession) -> None:
    await _runs(session, _mk_run("run-a", "shadow", "s", BASE))
    _snaps(session, "run-a", [10_000], BASE)
    await session.commit()

    r = await client.get("/api/v1/equity/account", params={"strategy": "nope"})
    assert r.status_code == 404

    r = await client.get("/api/v1/equity/account")  # strategy is required
    assert r.status_code == 422
