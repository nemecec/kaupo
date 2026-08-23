"""Shadow run end-to-end against Postgres with a scripted exchange client."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.core.runner import ShadowRequest, run_shadow
from kaupo.data.candles import get_candles, upsert_candles
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1

STRATEGY = """
from kaupo.sdk.protocol import StrategyBase

class Noop(StrategyBase):
    id = "noop"
    def on_candle(self, ctx):
        return []
"""


def hourly_candle(i: int, end: datetime) -> Candle:
    ts = end - timedelta(hours=i)
    return Candle(pair=PAIR, timeframe=TF, ts=ts, open=100, high=101, low=99, close=100, volume=1.0)


class ScriptedClient:
    """Serves backfill pages, then scripted poll batches; sets stop at the end."""

    def __init__(self, history: list[Candle], poll_batches: list[list[Candle]], stop) -> None:  # type: ignore[no-untyped-def]
        self.history = history
        self.poll_batches = poll_batches
        self.stop = stop
        self.in_backfill = True

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if self.in_backfill:
            if since is None:
                return []
            page = [c for c in self.history if c.ts >= since]
            if not page:
                self.in_backfill = False
            return page
        if self.poll_batches:
            return self.poll_batches.pop(0)
        self.stop.set()
        return []


async def test_shadow_run_processes_new_candles(session: AsyncSession, tmp_path: Path) -> None:
    import asyncio

    # history ends at the last full hour
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history_end = now - timedelta(hours=1)
    history = [hourly_candle(i, history_end) for i in reversed(range(120))]
    history.sort(key=lambda c: c.ts)
    await upsert_candles(session, history)
    await session.commit()

    new_candle = Candle(pair=PAIR, timeframe=TF, ts=now, open=101, high=102, low=100, close=101, volume=2.0)

    stop = asyncio.Event()
    client = ScriptedClient(
        history=history,
        poll_batches=[
            [history[-1]],  # duplicate of warm-up -> skipped
            [new_candle],  # genuinely new -> processed + persisted
        ],
        stop=stop,
    )

    (tmp_path / "noop.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["noop"]

    result = await run_shadow(
        ShadowRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
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
    assert runs[0].mode == "shadow"
    assert runs[0].status in ("completed", "halted")

    # exactly one processed candle -> one equity snapshot
    snapshots = (await session.execute(select(EquitySnapshotRow))).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].ts == now

    # the new candle was persisted by the chain
    stored = await get_candles(session, PAIR, TF, now, now + timedelta(hours=1))
    assert len(stored) == 1
    assert stored[0].close == 101
