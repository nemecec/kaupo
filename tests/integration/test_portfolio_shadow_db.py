"""Portfolio shadow run end-to-end against Postgres with a scripted exchange client."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.core.runner import PortfolioShadowRequest, run_portfolio_shadow
from kaupo.data.candles import get_candles, upsert_candles
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
PAIRS = [BTC, SOL]
TF = Timeframe.H1

STRATEGY = """
from kaupo.sdk.protocol import PortfolioStrategyBase

class Noop(PortfolioStrategyBase):
    id = "noop-portfolio"
    def on_candle(self, ctx):
        return []
"""


def hourly_candle(pair: Pair, i: int, end: datetime, price: float) -> Candle:
    ts = end - timedelta(hours=i)
    return Candle(
        pair=pair, timeframe=TF, ts=ts, open=price, high=price + 1, low=price - 1, close=price, volume=1.0
    )


class ScriptedUniverseClient:
    """Serves per-pair backfill pages, then per-pair poll batches; sets stop at the end."""

    def __init__(
        self,
        history: dict[Pair, list[Candle]],
        poll_batches: dict[Pair, list[list[Candle]]],
        stop: asyncio.Event,
    ) -> None:
        self.history = history
        self.poll_batches = {pair: list(batches) for pair, batches in poll_batches.items()}
        self.stop = stop
        self.in_backfill = set(history)
        self.stop_scheduled = False

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if pair in self.in_backfill:
            if since is None:
                return []
            page = [c for c in self.history[pair] if c.ts >= since]
            if not page:
                self.in_backfill.discard(pair)
            return page
        if self.poll_batches[pair]:
            return self.poll_batches[pair].pop(0)
        if all(not batches for batches in self.poll_batches.values()) and not self.stop_scheduled:
            # the pollers run ahead of the engine: give the engine a moment to
            # process the last joined step before the stop lands
            self.stop_scheduled = True
            asyncio.get_running_loop().call_later(0.5, self.stop.set)
        return []


async def test_portfolio_shadow_run_processes_new_candles(session: AsyncSession, tmp_path: Path) -> None:
    # history ends at the last full hour, per pair
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history_end = now - timedelta(hours=1)
    history: dict[Pair, list[Candle]] = {}
    for j, pair in enumerate(PAIRS):
        candles = [hourly_candle(pair, i, history_end, 100 * (j + 1)) for i in reversed(range(120))]
        candles.sort(key=lambda c: c.ts)
        history[pair] = candles
        await upsert_candles(session, candles)
    await session.commit()

    new_candles = {
        pair: Candle(pair=pair, timeframe=TF, ts=now, open=101, high=102, low=100, close=101, volume=2.0)
        for pair in PAIRS
    }

    stop = asyncio.Event()
    client = ScriptedUniverseClient(
        history=history,
        poll_batches={
            pair: [
                [history[pair][-1]],  # duplicate of warm-up -> skipped
                [new_candles[pair]],  # genuinely new -> processed + persisted
            ]
            for pair in PAIRS
        },
        stop=stop,
    )

    (tmp_path / "noop.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["noop-portfolio"]

    result = await run_portfolio_shadow(
        PortfolioShadowRequest(
            strategy=strategy,
            params={},
            pairs=[SOL, BTC],  # intentionally unsorted: the request canonicalizes
            timeframe=TF,
            warmup=50,
            poll_interval_seconds=0,
        ),
        get_sessionmaker(),
        client,  # type: ignore[arg-type]
        stop=stop,
    )

    assert result.status.value in ("completed", "halted")  # stream dried up / stop set

    runs = (await session.execute(select(RunRow))).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.mode == "shadow"
    assert run.status in ("completed", "halted")
    # the runs row keeps config["pair"] a plain string: the joined sorted list
    assert run.config["pair"] == "BTC/EUR,SOL/EUR"
    assert run.config["pairs"] == ["BTC/EUR", "SOL/EUR"]

    # one processed universe step -> one equity snapshot
    snapshots = (await session.execute(select(EquitySnapshotRow))).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].ts == now

    # the new candles were persisted by the chain, per pair
    for pair in PAIRS:
        stored = await get_candles(session, pair, TF, now, now + timedelta(hours=1))
        assert len(stored) == 1
        assert stored[0].close == 101
