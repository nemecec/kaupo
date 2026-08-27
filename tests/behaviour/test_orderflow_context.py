"""Behaviour: ticks()/book()/tick_flow() on the strategy context are
point-in-time at the virtual clock, for the single-pair and the portfolio
engine alike.

Canned candles + canned ticks/book, in-memory recorder — no DB, no network.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.orderflow import StaticOrderFlowProvider
from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import BookSnapshot, Candle, Pair, RunMode, TickFlow, Timeframe, TradeTick
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase
from kaupo.venues.paper import PaperVenue

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def tick(minutes: float, side: str = "buy", size: float = 1.0, pair: str = "BTC/EUR") -> TradeTick:
    return TradeTick(
        exchange="kraken", pair=pair, ts=BASE + timedelta(minutes=minutes), price=100.0, size=size, side=side
    )


def snapshot(hours: float, pair: str = "BTC/EUR") -> BookSnapshot:
    return BookSnapshot(
        exchange="kraken",
        pair=pair,
        ts=BASE + timedelta(hours=hours),
        bid=99.0,
        ask=101.0,
        bid_size=1.0,
        ask_size=2.0,
    )


# ticks straddling the candle-close grid: 60m lands exactly on a close
# (boundary must be inclusive for ticks, excluded from the still-open flow
# bucket), 300m mid-candle
TICKS = [
    tick(30, "buy", 1.0),
    tick(60, "buy", 1.0),
    tick(90, "sell", 2.0),
    tick(100, "buy", 3.0),
    tick(300, "sell", 5.0),
]
SOL_TICKS = [tick(45, "buy", 0.5, pair="SOL/EUR")]
BOOK = [snapshot(1), snapshot(2.5), snapshot(4.25), snapshot(20)]

BUCKET0 = TickFlow(ts=BASE, buy_count=1, sell_count=0, buy_volume=1.0, sell_volume=0.0, max_trade_size=1.0)
BUCKET1 = TickFlow(
    ts=BASE + timedelta(hours=1),
    buy_count=2,
    sell_count=1,
    buy_volume=4.0,
    sell_volume=2.0,
    max_trade_size=3.0,
)
BUCKET5 = TickFlow(
    ts=BASE + timedelta(hours=5),
    buy_count=0,
    sell_count=1,
    buy_volume=0.0,
    sell_volume=5.0,
    max_trade_size=5.0,
)


def candle(pair: Pair, i: int) -> Candle:
    p = 100.0 + i
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=p,
        high=p + 1,
        low=p - 1,
        close=p,
        volume=1.0,
    )


async def aiter(candles: list[Candle]) -> AsyncIterator[Candle]:
    for c in candles:
        yield c


# per candle i (close = BASE + (i+1)h), 6 candles
EXPECTED_TICKS = [TICKS[:2], TICKS[:4], TICKS[:4], TICKS[:4], TICKS, TICKS]
EXPECTED_FLOW = [
    [BUCKET0],
    [BUCKET0, BUCKET1],
    [BUCKET0, BUCKET1],
    [BUCKET0, BUCKET1],
    [BUCKET0, BUCKET1],
    [BUCKET0, BUCKET1, BUCKET5],
]
EXPECTED_BOOK = [BOOK[:1], BOOK[:1], BOOK[:2], BOOK[:2], BOOK[:3], BOOK[:3]]


class OrderFlowRecorder(StrategyBase):
    id = "orderflow-recorder"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.ticks_seen: list[list[TradeTick]] = []
        self.book_seen: list[list[BookSnapshot]] = []
        self.flow_seen: list[list[TickFlow]] = []
        self.latest_tick: list[list[TradeTick]] = []
        self.empty_n: list[bool] = []

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.ticks_seen.append(list(ctx.ticks(50)))
        self.book_seen.append(list(ctx.book(50)))
        self.flow_seen.append(list(ctx.tick_flow(50)))
        self.latest_tick.append(list(ctx.ticks(1)))
        self.empty_n.append(ctx.ticks(0) == [] and ctx.book(-1) == [] and ctx.tick_flow(0) == [])
        return []


def build_engine(
    recorder: InMemoryRecorder,
    strategy: StrategyBase,
    orderflow: StaticOrderFlowProvider | None = None,
) -> Engine:
    return Engine(
        strategy=strategy,
        venue=PaperVenue(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0),
        risk=RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=BTC, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id=strategy.id,
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
        orderflow=orderflow,
    )


async def test_engine_orderflow_is_point_in_time() -> None:
    recorder = InMemoryRecorder()
    strategy = OrderFlowRecorder(OrderFlowRecorder.params_schema())
    engine = build_engine(
        recorder,
        strategy,
        orderflow=StaticOrderFlowProvider(ticks={"BTC/EUR": TICKS}, book={"BTC/EUR": BOOK}),
    )
    result = await engine.run(aiter([candle(BTC, i) for i in range(6)]))

    assert result.num_fills == 0
    assert len(strategy.ticks_seen) == 6
    for i in range(6):
        assert strategy.ticks_seen[i] == EXPECTED_TICKS[i], f"candle {i} ticks"
        assert strategy.book_seen[i] == EXPECTED_BOOK[i], f"candle {i} book"
        assert strategy.flow_seen[i] == EXPECTED_FLOW[i], f"candle {i} flow"
    # ticks(1) returns the newest visible tick only
    assert strategy.latest_tick[0] == [TICKS[1]]  # the 60m tick, on the 1h close
    assert strategy.latest_tick[4] == [TICKS[4]]
    # the flow bucket of the just-closed candle is complete exactly at its
    # close: candle 0 shows bucket 0, and bucket 1 only appears at candle 1
    assert [len(f) for f in strategy.flow_seen] == [1, 2, 2, 2, 2, 3]
    assert all(strategy.empty_n)


async def test_engine_without_provider_serves_empty_orderflow() -> None:
    """Default wiring (parity): ticks/book/tick_flow are empty, behavior unchanged."""
    recorder = InMemoryRecorder()
    strategy = OrderFlowRecorder(OrderFlowRecorder.params_schema())
    engine = build_engine(recorder, strategy)  # no provider
    await engine.run(aiter([candle(BTC, i) for i in range(3)]))
    assert strategy.ticks_seen == [[], [], []]
    assert strategy.book_seen == [[], [], []]
    assert strategy.flow_seen == [[], [], []]


class PortfolioOrderFlowRecorder(PortfolioStrategyBase):
    id = "portfolio-orderflow-recorder"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.btc_flow: list[list[TickFlow]] = []
        self.sol_flow: list[list[TickFlow]] = []
        self.sol_ticks: list[list[TradeTick]] = []
        self.btc_book: list[list[BookSnapshot]] = []
        self.foreign_flow: list[list[TickFlow]] = []
        self.foreign_ticks: list[list[TradeTick]] = []

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.btc_flow.append(list(ctx.tick_flow(BTC, 50)))
        self.sol_flow.append(list(ctx.tick_flow(SOL, 50)))
        self.sol_ticks.append(list(ctx.ticks(SOL, 50)))
        self.btc_book.append(list(ctx.book(BTC, 50)))
        self.foreign_flow.append(list(ctx.tick_flow(Pair.parse("ADA/EUR"), 50)))
        self.foreign_ticks.append(list(ctx.ticks(Pair.parse("ADA/EUR"), 50)))
        return []


async def test_portfolio_engine_orderflow_per_pair_point_in_time() -> None:
    recorder = InMemoryRecorder()
    strategy = PortfolioOrderFlowRecorder(PortfolioOrderFlowRecorder.params_schema())
    engine = PortfolioEngine(
        strategy=strategy,
        venues={pair: PaperVenue(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0) for pair in (BTC, SOL)},
        risk=RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=PortfolioEngineConfig(pairs=(BTC, SOL), timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id=strategy.id,
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
        orderflow=StaticOrderFlowProvider(
            ticks={"BTC/EUR": TICKS, "SOL/EUR": SOL_TICKS}, book={"BTC/EUR": BOOK}
        ),
    )
    steps = list(
        joined_steps({BTC: [candle(BTC, i) for i in range(6)], SOL: [candle(SOL, i) for i in range(6)]})
    )

    async def step_stream() -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
        for step in steps:
            yield step

    await engine.run(step_stream())

    assert len(strategy.btc_flow) == 6
    for i in range(6):
        assert strategy.btc_flow[i] == EXPECTED_FLOW[i], f"step {i} BTC flow"
        assert strategy.btc_book[i] == EXPECTED_BOOK[i], f"step {i} BTC book"
    # SOL order flow is keyed by its own pair, independent of BTC's
    sol_bucket = TickFlow(
        ts=BASE, buy_count=1, sell_count=0, buy_volume=0.5, sell_volume=0.0, max_trade_size=0.5
    )
    for seen in strategy.sol_flow:
        assert seen == [sol_bucket]
    for seen in strategy.sol_ticks:
        assert seen == SOL_TICKS
    # pairs outside the universe get an empty series, not an error
    assert strategy.foreign_flow == [[]] * 6
    assert strategy.foreign_ticks == [[]] * 6
