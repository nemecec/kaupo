"""Golden-scenario behaviour tests for the example regime-switch strategy.

Synthetic, fully deterministic markets:
- a clean sine wave should be read as RANGING and traded mean-reversion-style
- a steady uptrend with higher highs should trigger momentum breakout entries
- identical inputs must produce identical outputs (determinism)
"""

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import Candle, Pair, RunMode, Side, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.loader import load_strategies
from kaupo.venues.paper import PaperVenue

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"


def market_candle(i: int, price: float, spread: float = 0.01) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=price,
        high=price * (1 + spread),
        low=price * (1 - spread),
        close=price,
        volume=1.0,
    )


def choppy_market(n: int = 300) -> list[Candle]:
    """Multi-frequency oscillation with no persistent trend -> RANGING regime."""
    freqs, amps = (0.5, 2.1, 5.7), (4.0, 2.0, 1.0)

    def price(i: int) -> float:
        return 100 + sum(a * math.sin(f * i + f) for f, a in zip(freqs, amps, strict=True))

    return [market_candle(i, price(i), spread=0.002) for i in range(n)]


def uptrend_market(n: int = 200) -> list[Candle]:
    # steady grind up with shallow higher lows -> persistent breakouts
    return [market_candle(i, 100 * (1.004**i), spread=0.002) for i in range(n)]


async def run_strategy(candles: list[Candle], params: dict | None = None) -> InMemoryRecorder:
    loaded = load_strategies(STRATEGIES_DIR)["regime-switch"]
    recorder = InMemoryRecorder()

    async def aiter() -> AsyncIterator[Candle]:
        for c in candles:
            yield c

    engine = Engine(
        strategy=loaded.create(params or {}),
        venue=PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5),
        risk=RiskManager(RiskConfig(max_position_quote=100_000, max_gross_exposure_quote=100_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1, starting_cash=10_000, liquidate_end=True),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id=loaded.id,
            strategy_version=loaded.version,
            strategy_source_hash=loaded.source_hash,
            config={},
        ),
    )
    await engine.run(aiter())
    return recorder


async def test_ranging_market_trades_mean_reversion() -> None:
    recorder = await run_strategy(choppy_market())
    buys = [f for f in recorder.fills if f.side is Side.BUY]
    sells = [f for f in recorder.fills if f.side is Side.SELL]
    assert len(buys) >= 2, "expected multiple mean-reversion entries in a choppy market"
    assert len(sells) >= 1

    # round trips in a clean oscillation should be net profitable after fees
    pnl = float(recorder.equity[-1][1]) - 10_000
    assert pnl > 0, f"expected profit in choppy market, got {pnl}"


async def test_trending_market_trades_momentum() -> None:
    recorder = await run_strategy(uptrend_market())
    buys = [f for f in recorder.fills if f.side is Side.BUY]
    assert len(buys) >= 1, "expected breakout entries in an uptrend"
    assert float(recorder.equity[-1][1]) > 10_000


async def test_deterministic() -> None:
    candles = choppy_market()
    first = await run_strategy(candles)
    second = await run_strategy(candles)
    assert [(f.ts, f.price, f.size, f.side) for f in first.fills] == [
        (f.ts, f.price, f.size, f.side) for f in second.fills
    ]
    assert [float(e[1]) for e in first.equity] == [float(e[1]) for e in second.equity]


async def test_flat_market_stays_out() -> None:
    # dead flat: no deviation, no breakouts -> no trades
    candles = [market_candle(i, 100.0, spread=0.001) for i in range(200)]
    recorder = await run_strategy(candles)
    assert recorder.fills == []
