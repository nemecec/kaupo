"""DbRecorder cross-flush order upsert + ledger_entries persistence."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.db.models import LedgerEntryRow, OrderRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import StrategyBase
    from kaupo.domain import OrderIntent, Side

    class BuyAndSell(StrategyBase):
        id = "buy-and-sell"
        def __init__(self, params):
            super().__init__(params)
            self.n = 0
        def on_candle(self, ctx):
            self.n += 1
            pair = ctx.candle.pair
            if self.n == 3:
                return [OrderIntent(pair=pair, side=Side.BUY, size=1.0)]
            if self.n == 7:
                return [OrderIntent(pair=pair, side=Side.SELL, size=1.0)]
            return []
    """
)


async def _run(session: AsyncSession, tmp_path: Path) -> str:
    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(12)
    ]
    await upsert_candles(session, candles)
    await session.commit()
    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["buy-and-sell"]
    run_id, _, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
        ),
        get_sessionmaker(),
    )
    return run_id


async def test_ledger_entries_persisted(session: AsyncSession, tmp_path: Path) -> None:
    run_id = await _run(session, tmp_path)
    entries = (
        (
            await session.execute(
                select(LedgerEntryRow).where(LedgerEntryRow.run_id == run_id).order_by(LedgerEntryRow.ts)
            )
        )
        .scalars()
        .all()
    )
    # deposit + buy (quote, base) + sell (quote, base)
    assert len(entries) == 5
    assert entries[0].reason == "deposit"
    assert entries[0].asset == "EUR"
    assert float(entries[0].balance_after) == 10_000.0
    assets = [(e.asset, e.reason) for e in entries[1:]]
    assert assets == [("EUR", "trade"), ("BTC", "trade"), ("EUR", "trade"), ("BTC", "trade")]
    # balances never negative
    assert all(float(e.balance_after) >= 0 for e in entries)


async def test_cross_flush_order_upsert(session: AsyncSession, tmp_path: Path) -> None:
    """flush_every=1 forces submit-state and filled-state into separate flushes;
    the SQL upsert must merge them into one final row."""
    from kaupo.core.recorder import DbRecorder

    candles = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1.0,
        )
        for i in range(12)
    ]
    await upsert_candles(session, candles)
    await session.commit()
    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["buy-and-sell"]

    # patch DbRecorder default flush_every to 1 for this test
    original_init = DbRecorder.__init__

    def patched(self, sm, flush_every=1):  # type: ignore[no-untyped-def]
        original_init(self, sm, flush_every)

    DbRecorder.__init__ = patched  # type: ignore[method-assign]
    try:
        run_id, _, _ = await run_backtest(
            BacktestRequest(
                strategy=strategy,
                params={},
                pair=PAIR,
                timeframe=Timeframe.H1,
                start=BASE,
                end=BASE + timedelta(hours=12),
            ),
            get_sessionmaker(),
        )
    finally:
        DbRecorder.__init__ = original_init  # type: ignore[method-assign]

    orders = (await session.execute(select(OrderRow).where(OrderRow.run_id == run_id))).scalars().all()
    assert len(orders) == 2  # upsert merged, not duplicated
    assert all(o.status == "filled" for o in orders)
    assert all(o.filled_price is not None for o in orders)


async def test_flush_failure_retains_buffers(session: AsyncSession) -> None:
    """A failed commit must not lose buffered rows; the next flush writes them."""
    from kaupo.core.recorder import DbRecorder, RunInfo
    from kaupo.domain import RunMode

    sessionmaker = get_sessionmaker()

    # wrap sessions so the first flush's commit fails once
    class FailingOnceSession:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self._inner = inner
            self.failed = False

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            await self._inner.close()

        async def execute(self, *a, **kw):  # type: ignore[no-untyped-def]
            return await self._inner.execute(*a, **kw)

        def add(self, obj):  # type: ignore[no-untyped-def]
            self._inner.add(obj)

        def add_all(self, objs):  # type: ignore[no-untyped-def]
            self._inner.add_all(objs)

        async def commit(self) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated commit failure")
            await self._inner.commit()

        async def rollback(self) -> None:
            await self._inner.rollback()

    state = {"fail": True}

    class WrappedMaker:
        def __call__(self):  # type: ignore[no-untyped-def]
            inner = sessionmaker()
            if state["fail"]:
                state["fail"] = False
                return FailingOnceSession(inner)
            return inner

    recorder = DbRecorder(sessionmaker)
    await recorder.start(
        RunInfo(
            mode=RunMode.BACKTEST, strategy_id="s", strategy_version="v", strategy_source_hash="x", config={}
        )
    )
    recorder._sessionmaker = WrappedMaker()  # type: ignore[assignment]  # fail next flush only

    from kaupo.domain import Order, OrderType, Side

    order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.MARKET, size=1.0)
    await recorder.record_order(order)
    with pytest.raises(RuntimeError):
        await recorder.flush()
    assert len(recorder._orders) == 1  # retained, not cleared

    await recorder.flush()  # second attempt succeeds
    assert recorder._orders == []

    row = (await session.execute(select(OrderRow).where(OrderRow.id == order.id))).scalar_one()
    assert row.run_id == recorder.run_id


async def test_start_halts_stale_same_strategy_shadow_runs(session: AsyncSession) -> None:
    """A starting shadow/live run supersedes stale "running" rows of the same
    strategy (dead processes from restarts). Other strategies and backtests
    stay untouched."""
    from kaupo.core.recorder import DbRecorder, RunInfo
    from kaupo.db.models import RunRow
    from kaupo.domain import RunMode

    sm = get_sessionmaker()

    def info(mode: RunMode, strategy_id: str) -> RunInfo:
        return RunInfo(
            mode=mode, strategy_id=strategy_id, strategy_version="v", strategy_source_hash="x", config={}
        )

    stale = DbRecorder(sm)
    await stale.start(info(RunMode.SHADOW, "s1"))
    other_strategy = DbRecorder(sm)
    await other_strategy.start(info(RunMode.SHADOW, "s2"))
    backtest_same_strategy = DbRecorder(sm)
    await backtest_same_strategy.start(info(RunMode.BACKTEST, "s1"))
    live = DbRecorder(sm)
    await live.start(info(RunMode.SHADOW, "s1"))

    rows = {r.id: r for r in (await session.execute(select(RunRow))).scalars().all()}
    assert rows[stale.run_id].status == "halted"
    assert rows[stale.run_id].ended_at is not None
    assert rows[stale.run_id].metrics["halt_reason"] == "superseded by a newer run of the same strategy"
    assert rows[other_strategy.run_id].status == "running"
    assert rows[backtest_same_strategy.run_id].status == "running"
    assert rows[live.run_id].status == "running"
