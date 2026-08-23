"""Parity: the same candles through backtest-style and shadow-style wiring
produce identical results — the engine is source-agnostic.

Also verifies warm-up semantics: history is prefilled, nothing is traded
or recorded during warm-up.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import Candle, OrderIntent, Pair, RunMode, Side, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import StrategyBase, StrategyContext
from kaupo.venues.paper import PaperVenue

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int) -> Candle:
    p = 100 + i
    return Candle(pair=PAIR, timeframe=Timeframe.H1, ts=BASE + timedelta(hours=i),
                  open=p, high=p + 1, low=p - 1, close=p, volume=1.0)


class BuyLowSellHigh(StrategyBase):
    id = "parity-script"
    seen_history: ClassVar[list[int]] = []

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.n = 0

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        self.n += 1
        BuyLowSellHigh.seen_history.append(len(ctx.history(10_000)))
        if self.n == 3:
            return [OrderIntent(pair=PAIR, side=Side.BUY, size=1.0)]
        if self.n == 7:
            return [OrderIntent(pair=PAIR, side=Side.SELL, size=1.0)]
        return []


def build(recorder: InMemoryRecorder, mode: RunMode) -> Engine:
    return Engine(
        strategy=BuyLowSellHigh(BuyLowSellHigh.params_schema()),
        venue=PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5),
        risk=RiskManager(RiskConfig(max_position_quote=100_000, max_gross_exposure_quote=100_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1, starting_cash=10_000),
        run_info=RunInfo(mode=mode, strategy_id="parity-script", strategy_version="v1",
                         strategy_source_hash="x", config={}),
    )


async def stream(candles: list[Candle]) -> AsyncIterator[Candle]:
    for c in candles:
        yield c


async def test_backtest_shadow_parity() -> None:
    candles = [candle(i) for i in range(12)]

    # backtest-style: single historical stream
    bt_rec = InMemoryRecorder()
    bt_result = await build(bt_rec, RunMode.BACKTEST).run(stream(candles))

    # shadow-style: warm-up chain + "live" stream, warmup=0
    sh_rec = InMemoryRecorder()
    sh_result = await build(sh_rec, RunMode.SHADOW).run(stream(candles), warmup=0)

    assert bt_result.status == sh_result.status
    assert [(f.ts, f.side, f.price, f.size, f.fee) for f in bt_rec.fills] == [
        (f.ts, f.side, f.price, f.size, f.fee) for f in sh_rec.fills
    ]
    assert [(ts, eq) for ts, eq, _, _ in bt_rec.equity] == [
        (ts, eq) for ts, eq, _, _ in sh_rec.equity
    ]


async def test_warmup_prefills_history_without_trading() -> None:
    BuyLowSellHigh.seen_history = []
    candles = [candle(i) for i in range(12)]
    rec = InMemoryRecorder()
    engine = build(rec, RunMode.SHADOW)
    await engine.run(stream(candles), warmup=5)

    # during warm-up nothing recorded: first equity snapshot is candle 5
    assert rec.equity[0][0] == BASE + timedelta(hours=5)
    # first strategy call already sees the 5 warm-up candles + the current one
    assert BuyLowSellHigh.seen_history[0] == 6
    # buy at n==3 (candle 7) fills at candle 8 open
    buys = [f for f in rec.fills if f.side is Side.BUY]
    assert buys[0].ts == BASE + timedelta(hours=8)
