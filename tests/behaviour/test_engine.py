"""Behaviour: canned candles through the engine produce exact expected trades.

Uses a scripted strategy and the in-memory recorder — no DB, no network.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import (
    Candle,
    OrderIntent,
    OrderStatus,
    OrderType,
    Pair,
    RunMode,
    RunStatus,
    Side,
    Timeframe,
)
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import StrategyBase
from kaupo.venues.paper import PaperVenue

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

# uptrend: open == close == 100 + i
PRICES = [100 + i for i in range(10)]


def candle(i: int) -> Candle:
    p = PRICES[i]
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=p,
        high=p + 1,
        low=p - 1,
        close=p,
        volume=1.0,
    )


class BuyAt3SellAt7(StrategyBase):
    id = "scripted"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.n = 0

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.n += 1
        if self.n == 3:
            return [OrderIntent(pair=PAIR, side=Side.BUY, size=1.0, reason="entry")]
        if self.n == 7:
            return [OrderIntent(pair=PAIR, side=Side.SELL, size=1.0, reason="exit")]
        return []


async def aiter(candles: list[Candle]) -> AsyncIterator[Candle]:
    for c in candles:
        yield c


def build_engine(
    recorder: InMemoryRecorder,
    risk: RiskManager | None = None,
    control_probe=None,  # type: ignore[no-untyped-def]
    candle_timeout_seconds: float = 120.0,
) -> Engine:
    return Engine(
        strategy=BuyAt3SellAt7(BuyAt3SellAt7.params_schema()),
        venue=PaperVenue(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0),  # zero-cost for exact math
        risk=risk or RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="scripted",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
        control_probe=control_probe,
        candle_timeout_seconds=candle_timeout_seconds,
    )


async def test_intents_fill_on_next_candle_open() -> None:
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    result = await engine.run(aiter([candle(i) for i in range(10)]))

    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 2

    buy, sell = recorder.fills
    # intent on candle 2 (close 102) fills at candle 3 open (103)
    assert buy.side is Side.BUY
    assert buy.price == 103.0
    assert buy.ts == BASE + timedelta(hours=3)
    # intent on candle 6 fills at candle 7 open (107)
    assert sell.side is Side.SELL
    assert sell.price == 107.0

    # 10_000 - 103 + 107
    assert float(result.final_equity) == pytest.approx(10_004.0)


class HangingRecorder(InMemoryRecorder):
    """Wedges inside record_equity — the 2026-08-31 silent-stall shape (kaupo#31)."""

    async def record_equity(self, ts, equity, cash, unrealized) -> None:  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()


async def test_candle_body_watchdog_fails_loudly() -> None:
    # a stuck candle body must surface as a failed run (the supervisor then
    # restarts it) instead of hanging silently for hours
    engine = build_engine(HangingRecorder(), candle_timeout_seconds=0.05)
    with pytest.raises(TimeoutError):
        await engine.run(aiter([candle(i) for i in range(3)]))


async def test_position_held_at_end_is_not_liquidated_by_default() -> None:
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    candles = [candle(i) for i in range(5)]  # buy at 3, no sell before end
    result = await engine.run(aiter(candles))
    assert result.num_fills == 1
    # position marked at last close (104): 10_000 - 103 + 104
    assert float(result.final_equity) == pytest.approx(10_001.0)


class LimitBuyExpireSell(StrategyBase):
    id = "limit-scripted"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.n = 0

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.n += 1
        if self.n == 1:
            # candle 1: open 101, low 100 -> touches 100.5, fills at the limit
            return [
                OrderIntent(
                    pair=PAIR,
                    side=Side.BUY,
                    size=1.0,
                    order_type=OrderType.LIMIT,
                    limit_price=100.5,
                    reason="passive entry",
                )
            ]
        if self.n == 3:
            # candle 3: high 104 never reaches 999 -> expires at that close
            return [
                OrderIntent(
                    pair=PAIR,
                    side=Side.SELL,
                    size=1.0,
                    order_type=OrderType.LIMIT,
                    limit_price=999.0,
                    reason="passive exit, never touched",
                )
            ]
        return []


async def test_limit_orders_fill_at_maker_and_expire_untouched() -> None:
    recorder = InMemoryRecorder()
    engine = Engine(
        strategy=LimitBuyExpireSell(LimitBuyExpireSell.params_schema()),
        venue=PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5),
        risk=RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="limit-scripted",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )
    result = await engine.run(aiter([candle(i) for i in range(6)]))

    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 1

    (buy,) = recorder.fills
    assert buy.side is Side.BUY
    assert buy.price == 100.5  # the limit, not the (higher) open, no slippage
    assert buy.fee == pytest.approx(100.5 * 0.0016)  # maker fee
    assert buy.ts == BASE + timedelta(hours=1)

    # audit trail: both orders recorded at submit and again at resolution
    assert len(recorder.orders) == 4
    statuses = {o.id: o.status for o in recorder.orders}
    assert statuses[buy.order_id] is OrderStatus.FILLED
    (expired_id,) = {oid for oid in statuses if oid != buy.order_id}
    assert statuses[expired_id] is OrderStatus.CANCELLED  # expired untouched

    # 10_000 - 100.5 - fee, position marked at last close (105)
    assert float(result.final_equity) == pytest.approx(10_000 - 100.5 * 1.0016 + 105)


async def test_daily_loss_halts_run() -> None:
    recorder = InMemoryRecorder()
    risk = RiskManager(
        RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000, max_daily_loss_quote=1.0)
    )
    engine = build_engine(recorder, risk=risk)

    # falling prices: buy at 3, equity drops below daily loss
    falling = [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=100 - i,
            high=101 - i,
            low=99 - i,
            close=100 - i,
            volume=1.0,
        )
        for i in range(10)
    ]
    result = await engine.run(aiter(falling))
    assert result.status is RunStatus.HALTED
    assert "max daily loss" in result.halt_reason


async def test_equity_snapshots_recorded_each_candle() -> None:
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    await engine.run(aiter([candle(i) for i in range(10)]))
    assert len(recorder.equity) == 10
    assert recorder.equity[-1][1] == pytest.approx(10_004.0)


async def test_oversized_buy_is_resized_and_fills_without_crashing() -> None:
    """An all-in (and then some) buy must be fee-aware resized, not crash."""
    from kaupo.domain import OrderIntent

    class AllIn(StrategyBase):
        id = "all-in"

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            if len(ctx.history(1000)) == 3:
                return [OrderIntent(pair=PAIR, side=Side.BUY, size=10_000.0)]
            return []

    recorder = InMemoryRecorder()
    engine = Engine(
        strategy=AllIn(AllIn.params_schema()),
        venue=PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5),
        risk=RiskManager(
            RiskConfig(
                max_position_quote=1_000_000,
                max_gross_exposure_quote=1_000_000,
                taker_fee_bps=26,
                slippage_bps=5,
            )
        ),
        ledger=Ledger("EUR", 1_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="all-in",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )
    result = await engine.run(aiter([candle(i) for i in range(10)]))
    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 1
    # affordable size = 1000 / (1 + 0.0031) / 103 -> ~9.66; cost incl. fee <= 1000
    fill = recorder.fills[0]
    assert fill.size * fill.price * (1 + 0.0026) <= 1_000.0
    assert 0 < float(result.final_equity) < 1_100


class TestControlAndFailureWiring:
    async def test_rejected_intent_reaches_no_venue(self) -> None:
        class DustBuyer(StrategyBase):
            id = "dust"

            def on_candle(self, ctx):  # type: ignore[no-untyped-def]
                return [OrderIntent(pair=PAIR, side=Side.BUY, size=0.00001)]

        recorder = InMemoryRecorder()
        engine = Engine(
            strategy=DustBuyer(DustBuyer.params_schema()),
            venue=PaperVenue(26, 16, 5),
            risk=RiskManager(RiskConfig()),
            ledger=Ledger("EUR", 10_000.0, BASE),
            recorder=recorder,
            config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
            run_info=RunInfo(
                mode=RunMode.BACKTEST,
                strategy_id="dust",
                strategy_version="v1",
                strategy_source_hash="x",
                config={},
            ),
        )
        result = await engine.run(aiter([candle(i) for i in range(5)]))
        assert result.status is RunStatus.COMPLETED
        assert recorder.orders == []
        assert recorder.fills == []

    async def test_strategy_exception_fails_run_and_finishes(self) -> None:
        class Boom(StrategyBase):
            id = "boom"

            def on_candle(self, ctx):  # type: ignore[no-untyped-def]
                raise RuntimeError("strategy exploded")

        recorder = InMemoryRecorder()
        engine = build_engine(recorder)
        engine.strategy = Boom(Boom.params_schema())
        try:
            result = await engine.run(aiter([candle(i) for i in range(3)]))
            assert result.status is RunStatus.FAILED
        except RuntimeError:
            pass  # engine re-raises after finishing
        assert recorder.final_status is RunStatus.FAILED

    async def test_kill_stops_at_next_candle(self) -> None:
        from kaupo.core.runner import DbControlProbe  # noqa: F401

        commands = iter([None, None, "kill"])

        async def probe() -> str | None:
            return next(commands, "kill")

        recorder = InMemoryRecorder()
        engine = build_engine(recorder, control_probe=probe)
        result = await engine.run(aiter([candle(i) for i in range(10)]))
        assert result.status is RunStatus.HALTED
        assert result.halt_reason == "killed via control API"

    async def test_switch_halts_like_kill_with_switch_reason(self) -> None:
        commands = iter([None, None, "switch"])

        async def probe() -> str | None:
            return next(commands, "switch")

        recorder = InMemoryRecorder()
        engine = build_engine(recorder, control_probe=probe)
        result = await engine.run(aiter([candle(i) for i in range(10)]))
        assert result.status is RunStatus.HALTED
        assert result.halt_reason == "strategy switch requested"
        assert recorder.final_status is RunStatus.HALTED

    async def test_pause_skips_strategy_but_keeps_history(self) -> None:
        seen: list[int] = []

        class Observer(BuyAt3SellAt7):
            def on_candle(self, ctx):  # type: ignore[no-untyped-def]
                seen.append(len(ctx.history(10_000)))
                return []

        async def probe() -> str | None:
            return "pause"

        recorder = InMemoryRecorder()
        engine = build_engine(recorder, control_probe=probe)
        engine.strategy = Observer(Observer.params_schema())
        result = await engine.run(aiter([candle(i) for i in range(5)]))
        assert result.status is RunStatus.COMPLETED
        assert seen == []  # strategy never called while paused
        assert len(engine.history) == 5  # history still advanced
        assert len(recorder.equity) == 5  # equity still recorded


async def test_ledger_rejection_beyond_cushion_keeps_everything_consistent() -> None:
    """Next open gaps >1% above the decision close: the fee-aware size still
    doesn't cover it; the ledger rejects, venue rolls back, run completes."""
    from kaupo.domain import OrderIntent

    class AllIn(StrategyBase):
        id = "all-in-gap"

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            if len(ctx.history(1000)) == 2:
                return [OrderIntent(pair=PAIR, side=Side.BUY, size=10_000.0)]
            return []

    def gap_candle(i: int) -> Candle:
        p = 100 if i < 2 else 104  # 4% gap at candle 2, where the buy fills
        return Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=p,
            high=p + 1,
            low=p - 1,
            close=p,
            volume=1.0,
        )

    risk = RiskManager(RiskConfig(max_position_quote=1_000_000, max_gross_exposure_quote=1_000_000))
    recorder = InMemoryRecorder()
    engine = Engine(
        strategy=AllIn(AllIn.params_schema()),
        venue=PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5),
        risk=risk,
        ledger=Ledger("EUR", 1_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="all-in-gap",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )
    result = await engine.run(aiter([gap_candle(i) for i in range(8)]))
    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 0
    assert any("ledger rejected" in r for r in risk.rejections)
    assert engine.venue._positions.get(PAIR, 0.0) == 0.0  # rolled back
    rejected_orders = [o for o in recorder.orders if o.status.value == "rejected"]
    assert len({o.id for o in rejected_orders}) == 1  # recorded at submit + after void


async def test_liquidate_end_records_no_duplicate_snapshot() -> None:
    """Open position + liquidate_end: exactly one snapshot per candle ts."""
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    engine.config = EngineConfig(pair=PAIR, timeframe=Timeframe.H1, liquidate_end=True)
    await engine.run(aiter([candle(i) for i in range(5)]))
    assert len(recorder.equity) == 5
    assert len({ts for ts, *_ in recorder.equity}) == 5


async def test_stop_event_halts_before_any_processing() -> None:
    import asyncio

    stop = asyncio.Event()
    stop.set()
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    result = await engine.run(aiter([candle(i) for i in range(5)]), stop=stop)
    assert result.status is RunStatus.HALTED
    assert result.halt_reason == "stopped externally"
    assert recorder.equity == []
