"""Behaviour: canned candles through the engine produce exact expected trades.

Uses a scripted strategy and the in-memory recorder — no DB, no network.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import (
    Candle,
    OrderIntent,
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


def build_engine(recorder: InMemoryRecorder, risk: RiskManager | None = None) -> Engine:
    return Engine(
        strategy=BuyAt3SellAt7(BuyAt3SellAt7.params_schema()),
        venue=PaperVenue(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0),  # zero-cost for exact math
        risk=risk or RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1, starting_cash=10_000),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="scripted",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
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


async def test_position_held_at_end_is_not_liquidated_by_default() -> None:
    recorder = InMemoryRecorder()
    engine = build_engine(recorder)
    candles = [candle(i) for i in range(5)]  # buy at 3, no sell before end
    result = await engine.run(aiter(candles))
    assert result.num_fills == 1
    # position marked at last close (104): 10_000 - 103 + 104
    assert float(result.final_equity) == pytest.approx(10_001.0)


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
