"""Buy-and-hold benchmark series on the run equity endpoint, over canned candles."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import get_settings
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import dispose_engine
from kaupo.domain import Candle, Pair, Timeframe, new_id

pytestmark = pytest.mark.integration

BTC = Pair.parse("BTC/EUR")
ETH = Pair.parse("ETH/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


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


def _candle(pair: Pair, hour: int, close: float) -> Candle:
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + hour * HOUR,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


async def _seed_run(session: AsyncSession, run_id: str, config: dict, n_snapshots: int) -> None:
    session.add(
        RunRow(
            id=run_id,
            mode="backtest",
            strategy_id="s",
            strategy_version="v1",
            started_at=BASE,
            ended_at=BASE + n_snapshots * HOUR,
            status="completed",
            config=config,
        )
    )
    await session.flush()  # run must exist before dependent snapshots (FK)
    for i in range(n_snapshots):
        session.add(
            EquitySnapshotRow(
                id=new_id(),
                run_id=run_id,
                ts=BASE + i * HOUR,
                equity=1_000.0,
                cash=1_000.0,
                unrealized_pnl=0.0,
            )
        )
    await session.commit()


async def _equity(client: AsyncClient, run_id: str) -> dict:
    r = await client.get(f"/api/v1/runs/{run_id}/equity")
    assert r.status_code == 200
    return r.json()


async def test_single_pair_benchmark_exact_values(client: AsyncClient, session: AsyncSession) -> None:
    # shadow-style config: no exchange key, the default must kick in
    await upsert_candles(session, [_candle(BTC, i, close) for i, close in enumerate([100, 110, 90, 121])])
    await _seed_run(
        session, "bench-single", {"pair": "BTC/EUR", "timeframe": "1h", "starting_cash": 1_000}, 4
    )

    body = await _equity(client, "bench-single")

    assert len(body["points"]) == 4
    # benchmark shares the equity snapshot timestamps
    assert [p["ts"] for p in body["benchmark"]] == [p["ts"] for p in body["points"]]
    # 1000 * close / 100
    assert [p["value"] for p in body["benchmark"]] == [1_000.0, 1_100.0, 900.0, 1_210.0]


async def test_portfolio_benchmark_equal_weight(client: AsyncClient, session: AsyncSession) -> None:
    # BTC/EUR hourly: 100, 110, 90, 100 — 500 cash buys 5 units at the first close
    await upsert_candles(session, [_candle(BTC, i, close) for i, close in enumerate([100, 110, 90, 100])])
    # ETH/EUR enters one hour late (no candle at BASE) and has a gap at +2h:
    # 500 cash buys 12.5 units at the first in-window close (40 at +1h)
    await upsert_candles(session, [_candle(ETH, 1, 40), _candle(ETH, 3, 60)])
    await _seed_run(
        session,
        "bench-portfolio",
        {
            "pair": "BTC/EUR,ETH/EUR",
            "pairs": ["BTC/EUR", "ETH/EUR"],
            "timeframe": "1h",
            "exchange": "kraken",
            "starting_cash": 1_000,
        },
        4,
    )

    body = await _equity(client, "bench-portfolio")

    assert [p["ts"] for p in body["benchmark"]] == [p["ts"] for p in body["points"]]
    assert [p["value"] for p in body["benchmark"]] == [
        1_000.0,  # 5*100 + 500 (ETH leg not bought yet, still cash)
        1_050.0,  # 5*110 + 12.5*40
        950.0,  # 5*90  + 12.5*40 (stale carry over the +2h gap)
        1_250.0,  # 5*100 + 12.5*60
    ]


async def test_benchmark_empty_when_no_candles_in_window(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_run(
        session, "bench-empty", {"pair": "DOGE/EUR", "timeframe": "1h", "starting_cash": 1_000}, 3
    )

    body = await _equity(client, "bench-empty")

    assert len(body["points"]) == 3  # snapshots are unaffected
    assert body["benchmark"] == []


async def test_benchmark_empty_when_no_snapshots(client: AsyncClient, session: AsyncSession) -> None:
    await upsert_candles(session, [_candle(BTC, 0, 100)])
    await _seed_run(session, "bench-no-snaps", {"pair": "BTC/EUR", "timeframe": "1h"}, 0)

    body = await _equity(client, "bench-no-snaps")

    assert body == {"points": [], "benchmark": []}
