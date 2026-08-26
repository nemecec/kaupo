"""Portfolio parity: the same candles through portfolio-backtest-style and
portfolio-shadow-style wiring produce identical fills and equity — the
engine is source-agnostic, and the live universe joiner emits exactly the
steps the backtest's timestamp join emits.

The universe has a data hole (SOL misses one candle in the live window), so
both wirings see the same partial step: the pair skips the tick and the
engine stale-carries its last close.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.core.runner import UniverseCandleJoiner
from kaupo.domain import Candle, OrderIntent, Pair, RunMode, Side, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import PortfolioContext, PortfolioStrategyBase
from kaupo.venues.paper import PaperVenue

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
ADA = Pair.parse("ADA/EUR")
PAIRS = (BTC, SOL, ADA)
BASE = datetime(2026, 1, 1, tzinfo=UTC)
N_CANDLES = 12
N_WARMUP = 5
SOL_GAP = 8  # SOL has no candle at this index (in the live window)


def series(pair: Pair, base_price: float, skip: set[int] | None = None) -> list[Candle]:
    candles = []
    for i in range(N_CANDLES):
        if skip and i in skip:
            continue
        p = base_price + i
        candles.append(
            Candle(
                pair=pair,
                timeframe=Timeframe.H1,
                ts=BASE + timedelta(hours=i),
                open=p,
                high=p + 1,
                low=p - 1,
                close=p,
                volume=1.0,
            )
        )
    return candles


def candles_by_pair() -> dict[Pair, list[Candle]]:
    return {
        BTC: series(BTC, 100),
        SOL: series(SOL, 50, skip={SOL_GAP}),
        ADA: series(ADA, 10),
    }


class ThreePairScript(PortfolioStrategyBase):
    id = "three-pair-script"
    seen_pairs: ClassVar[list[frozenset[Pair]]] = []
    seen_history: ClassVar[list[tuple[int, int, int]]] = []

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.n = 0

    def on_candle(self, ctx: PortfolioContext) -> list[OrderIntent]:
        self.n += 1
        ThreePairScript.seen_pairs.append(frozenset(ctx.candles))
        ThreePairScript.seen_history.append(
            (len(ctx.history(BTC, 10_000)), len(ctx.history(SOL, 10_000)), len(ctx.history(ADA, 10_000)))
        )
        if self.n == 2:
            return [OrderIntent(pair=BTC, side=Side.BUY, size=1.0, reason="btc entry")]
        if self.n == 3:
            return [OrderIntent(pair=SOL, side=Side.BUY, size=2.0, reason="sol entry")]
        if self.n == 5:
            return [OrderIntent(pair=BTC, side=Side.SELL, size=1.0, reason="btc exit")]
        if self.n == 6:
            return [OrderIntent(pair=SOL, side=Side.SELL, size=2.0, reason="sol exit")]
        return []


def build(recorder: InMemoryRecorder, mode: RunMode) -> PortfolioEngine:
    return PortfolioEngine(
        strategy=ThreePairScript(ThreePairScript.params_schema()),
        venues={pair: PaperVenue(taker_fee_bps=26, maker_fee_bps=16, slippage_bps=5) for pair in PAIRS},
        risk=RiskManager(RiskConfig(max_position_quote=100_000, max_gross_exposure_quote=100_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=PortfolioEngineConfig(pairs=PAIRS, timeframe=Timeframe.H1, liquidate_end=False),
        run_info=RunInfo(
            mode=mode,
            strategy_id="three-pair-script",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )


async def aiter(
    steps: list[tuple[datetime, dict[Pair, Candle]]],
) -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
    for step in steps:
        yield step


class QueuePoller:
    """LiveCandlePoller double: the test feeds candles through a queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Candle] = asyncio.Queue()

    async def feed(self, candle: Candle) -> None:
        await self.queue.put(candle)

    async def stream(self, stop: asyncio.Event | None = None) -> AsyncIterator[Candle]:
        while stop is None or not stop.is_set():
            try:
                candle = await asyncio.wait_for(self.queue.get(), timeout=0.02)
            except TimeoutError:
                continue
            yield candle


async def test_portfolio_backtest_shadow_parity() -> None:
    by_pair = candles_by_pair()
    cutoff = BASE + timedelta(hours=N_WARMUP)
    warmup_by_pair = {pair: [c for c in candles if c.ts < cutoff] for pair, candles in by_pair.items()}
    live_by_pair = {pair: [c for c in candles if c.ts >= cutoff] for pair, candles in by_pair.items()}

    # backtest-style: one timestamp-joined historical stream
    ThreePairScript.seen_pairs = []
    ThreePairScript.seen_history = []
    bt_rec = InMemoryRecorder()
    bt_result = await build(bt_rec, RunMode.BACKTEST).run(aiter(list(joined_steps(by_pair))), warmup=N_WARMUP)
    bt_seen_pairs, bt_seen_history = ThreePairScript.seen_pairs, ThreePairScript.seen_history

    # shadow-style: warm-up join + live streams joined by the universe joiner
    ThreePairScript.seen_pairs = []
    ThreePairScript.seen_history = []

    pollers = {pair: QueuePoller() for pair in PAIRS}
    joiner = UniverseCandleJoiner(pollers, grace_seconds=0.02)  # type: ignore[arg-type]
    stop = asyncio.Event()

    async def shadow_stream() -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
        for ts, step in joined_steps(warmup_by_pair):
            yield ts, dict(step)
        async for ts, step in joiner.stream(stop):
            yield ts, step

    async def feed() -> None:
        for pair, candles in live_by_pair.items():
            for candle in candles:
                await pollers[pair].feed(candle)
        await asyncio.sleep(0.2)  # let the grace window of the gap tick pass
        stop.set()

    sh_rec = InMemoryRecorder()
    sh_engine = build(sh_rec, RunMode.SHADOW)
    feeder = asyncio.create_task(feed())
    sh_result = await sh_engine.run(shadow_stream(), stop=stop, warmup=N_WARMUP)
    await feeder

    assert bt_result.status == sh_result.status
    assert bt_result.num_fills == sh_result.num_fills == 4
    assert [(f.ts, f.pair, f.side, f.price, f.size, f.fee) for f in bt_rec.fills] == [
        (f.ts, f.pair, f.side, f.price, f.size, f.fee) for f in sh_rec.fills
    ]
    assert [(ts, eq) for ts, eq, _, _ in bt_rec.equity] == [(ts, eq) for ts, eq, _, _ in sh_rec.equity]
    assert len(bt_rec.equity) == N_CANDLES - N_WARMUP

    # the strategy saw identical steps: same pairs per call (the SOL gap
    # tick included), same per-pair history at every call
    assert bt_seen_pairs == ThreePairScript.seen_pairs
    assert bt_seen_history == ThreePairScript.seen_history
    assert frozenset({BTC, ADA}) in bt_seen_pairs  # the gap tick: SOL skipped it in both modes
