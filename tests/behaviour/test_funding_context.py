"""Behaviour: funding() on the strategy context is point-in-time at the
virtual clock, for the single-pair and the portfolio engine alike.

Canned candles + canned funding, in-memory recorder — no DB, no network.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from kaupo.core.engine import Engine, EngineConfig
from kaupo.core.funding import StaticFundingProvider
from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
from kaupo.core.recorder import InMemoryRecorder, RunInfo
from kaupo.domain import Candle, FundingRate, Pair, RunMode, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import PortfolioStrategyBase, StrategyBase
from kaupo.venues.paper import PaperVenue

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

# funding points straddling the candle-close grid: 2h lands exactly on a
# close (boundary must be inclusive), 5.5h and 9.5h between closes, 20h
# beyond the last candle
FUNDING = [
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=2), rate=0.0001),
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=5, minutes=30), rate=-0.0002),
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=9, minutes=30), rate=0.0003),
    FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=20), rate=0.0004),
]
SOL_FUNDING = [
    FundingRate(exchange="binance", base_asset="SOL", ts=BASE + timedelta(hours=3), rate=0.0005),
]


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


def expected_point_in_time(i: int, points: Sequence[FundingRate] = FUNDING) -> list[FundingRate]:
    """Points visible at candle i: funding time at or before the close (ts + 1h)."""
    close = BASE + timedelta(hours=i + 1)
    return [r for r in points if r.ts <= close]


class FundingRecorder(StrategyBase):
    id = "funding-recorder"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.seen: list[list[FundingRate]] = []
        self.latest_one: list[list[FundingRate]] = []

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.seen.append(list(ctx.funding(50)))
        self.latest_one.append(list(ctx.funding(1)))
        return []


def build_engine(
    recorder: InMemoryRecorder,
    strategy: StrategyBase,
    funding: StaticFundingProvider | None = None,
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
        funding=funding,
    )


async def test_engine_funding_is_point_in_time() -> None:
    recorder = InMemoryRecorder()
    strategy = FundingRecorder(FundingRecorder.params_schema())
    engine = build_engine(recorder, strategy, funding=StaticFundingProvider({"BTC": FUNDING}))
    result = await engine.run(aiter([candle(BTC, i) for i in range(10)]))

    assert result.num_fills == 0
    assert len(strategy.seen) == 10
    for i, seen in enumerate(strategy.seen):
        assert seen == expected_point_in_time(i), f"candle {i}"
    # the series grows as the clock crosses funding times, always ascending
    assert [len(s) for s in strategy.seen] == [0, 1, 1, 1, 1, 2, 2, 2, 2, 3]
    # funding(1) returns the newest visible point only
    assert strategy.latest_one[5] == [FUNDING[1]]
    assert strategy.latest_one[9] == [FUNDING[2]]


async def test_engine_without_provider_serves_empty_funding() -> None:
    """Default wiring (parity): funding() is empty, behavior unchanged."""
    recorder = InMemoryRecorder()
    strategy = FundingRecorder(FundingRecorder.params_schema())
    engine = build_engine(recorder, strategy)  # no provider
    await engine.run(aiter([candle(BTC, i) for i in range(3)]))
    assert strategy.seen == [[], [], []]


class PortfolioFundingRecorder(PortfolioStrategyBase):
    id = "portfolio-funding-recorder"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.btc_seen: list[list[FundingRate]] = []
        self.sol_seen: list[list[FundingRate]] = []
        self.foreign_seen: list[list[FundingRate]] = []

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.btc_seen.append(list(ctx.funding(BTC, 50)))
        self.sol_seen.append(list(ctx.funding(SOL, 50)))
        self.foreign_seen.append(list(ctx.funding(Pair.parse("ADA/EUR"), 50)))
        return []


async def test_portfolio_engine_funding_per_pair_point_in_time() -> None:
    recorder = InMemoryRecorder()
    strategy = PortfolioFundingRecorder(PortfolioFundingRecorder.params_schema())
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
        funding=StaticFundingProvider({"BTC": FUNDING, "SOL": SOL_FUNDING}),
    )
    steps = list(
        joined_steps({BTC: [candle(BTC, i) for i in range(6)], SOL: [candle(SOL, i) for i in range(6)]})
    )

    async def step_stream() -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
        for step in steps:
            yield step

    await engine.run(step_stream())

    assert len(strategy.btc_seen) == 6
    for i, seen in enumerate(strategy.btc_seen):
        assert seen == expected_point_in_time(i), f"step {i}"
    # SOL funding is keyed by its own base asset, independent of BTC's
    for i, seen in enumerate(strategy.sol_seen):
        assert seen == expected_point_in_time(i, SOL_FUNDING), f"step {i}"
    # pairs outside the universe get an empty series, not an error
    assert strategy.foreign_seen == [[]] * 6
