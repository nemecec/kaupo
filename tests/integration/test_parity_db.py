"""THE parity test: backtest and shadow over identical candle histories must
produce identical fills and equity — in the production configuration
(backtest with history prefill, shadow with warm-up)."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.core.runner import ShadowRequest, run_shadow
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, FillRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1
N_WARMUP = 100
N_TRADE = 100

STRATEGY = """
from kaupo.sdk.protocol import StrategyBase
from kaupo.sdk import indicators as ind
from kaupo.domain import OrderIntent, Side

class ZScoreMr(StrategyBase):
    id = "zscore-mr"
    def on_candle(self, ctx):
        hist = ctx.history(30)
        if len(hist) < 30:
            return []
        closes = ind.closes(hist)
        mid = ind.sma(closes, 20)
        std = ind.rolling_std(closes, 20)
        z = (closes[-1] - mid[-1]) / std[-1]
        pos = ctx.position()
        if pos.size == 0 and z <= -1.0:
            size = (ctx.equity() * 0.5) / closes[-1]
            return [OrderIntent(pair=ctx.candle.pair, side=Side.BUY, size=size, reason="mr entry")]
        if pos.size > 0 and closes[-1] >= mid[-1]:
            return [OrderIntent(pair=ctx.candle.pair, side=Side.SELL, size=pos.size, reason="mr exit")]
        return []
"""


def make_candles() -> list[Candle]:
    """200 hourly candles ending at the last full hour, oscillating price."""
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    freqs, amps = (0.5, 2.1), (4.0, 2.0)
    candles = []
    for i in range(N_WARMUP + N_TRADE):
        price = 100 + sum(a * math.sin(f * i + f) for f, a in zip(freqs, amps, strict=True))
        candles.append(
            Candle(
                pair=PAIR,
                timeframe=TF,
                ts=end - timedelta(hours=N_WARMUP + N_TRADE - 1 - i),
                open=price,
                high=price * 1.002,
                low=price * 0.998,
                close=price,
                volume=1.0,
            )
        )
    return candles


class ScriptedClient:
    """No backfill pages; poller serves the trade-window candles one by one."""

    def __init__(self, live_candles: list[Candle], stop: asyncio.Event) -> None:
        self._batches: list[list[Candle]] = [[c] for c in live_candles]
        self._stop = stop
        self._in_backfill = True

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if self._in_backfill:
            self._in_backfill = False
            return []
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return []


async def test_backtest_shadow_parity_in_production_config(
    session: AsyncSession, tmp_path: Path
) -> None:
    candles = make_candles()
    warmup_candles = candles[:N_WARMUP]
    trade_candles = candles[N_WARMUP:]

    # shadow starts with only warm-up history in the store; the trade window
    # arrives "live" through the poller and is persisted by the chain
    await upsert_candles(session, warmup_candles)
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["zscore-mr"]
    sessionmaker = get_sessionmaker()

    stop = asyncio.Event()
    shadow_result = await run_shadow(
        ShadowRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=TF,
            warmup=N_WARMUP,
            poll_interval_seconds=0,
        ),
        sessionmaker,
        ScriptedClient(trade_candles, stop),  # type: ignore[arg-type]
        stop=stop,
    )

    bt_run_id, bt_result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=TF,
            start=trade_candles[0].ts,
            end=trade_candles[-1].ts + timedelta(hours=1),
            lookback=N_WARMUP,
            liquidate_end=False,  # shadow keeps positions open; liquidation is separate
        ),
        sessionmaker,
    )

    # find the shadow run id (the only shadow run in the DB)
    from kaupo.db.models import RunRow

    shadow_run = (
        (await session.execute(select(RunRow).where(RunRow.mode == "shadow"))).scalars().one()
    )

    def key(row) -> tuple:  # type: ignore[no-untyped-def]
        return (row.ts, row.side, round(row.price, 8), round(row.size, 8), round(row.fee, 8))

    shadow_fills = (
        (await session.execute(select(FillRow).where(FillRow.run_id == shadow_run.id).order_by(FillRow.ts)))
        .scalars()
        .all()
    )
    bt_fills = (
        (await session.execute(select(FillRow).where(FillRow.run_id == bt_run_id).order_by(FillRow.ts)))
        .scalars()
        .all()
    )

    assert len(shadow_fills) > 0, "parity test is vacuous without trades"
    assert [key(f) for f in shadow_fills] == [key(f) for f in bt_fills]

    shadow_equity = (
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == shadow_run.id)
                .order_by(EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )
    bt_equity = (
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == bt_run_id)
                .order_by(EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )
    assert [(s.ts, round(s.equity, 6)) for s in shadow_equity] == [
        (s.ts, round(s.equity, 6)) for s in bt_equity
    ]
    assert shadow_result.num_fills == bt_result.num_fills
