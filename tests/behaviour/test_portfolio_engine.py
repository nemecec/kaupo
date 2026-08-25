"""Behaviour: scripted portfolio strategies over hand-built multi-pair candles
produce exact expected fills and equity — no DB, no network."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
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
from kaupo.sdk.protocol import PortfolioStrategyBase
from kaupo.venues.paper import PaperVenue

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(pair: Pair, i: int, price: float) -> Candle:
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1.0,
    )


def series(pair: Pair, base: float, n: int, skip: set[int] | None = None) -> list[Candle]:
    return [candle(pair, i, base + i) for i in range(n) if not (skip and i in skip)]


async def aiter(
    steps: list[tuple[datetime, dict[Pair, Candle]]],
) -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
    for step in steps:
        yield step


def steps_of(candles_by_pair: dict[Pair, list[Candle]]) -> list[tuple[datetime, dict[Pair, Candle]]]:
    return list(joined_steps(candles_by_pair))


def build_engine(
    recorder: InMemoryRecorder,
    strategy: PortfolioStrategyBase,
    pairs: tuple[Pair, ...] = (BTC, SOL),
    risk: RiskManager | None = None,
    liquidate_end: bool = False,
) -> PortfolioEngine:
    return PortfolioEngine(
        strategy=strategy,
        venues={pair: PaperVenue(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0) for pair in pairs},
        risk=risk or RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000)),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=PortfolioEngineConfig(pairs=pairs, timeframe=Timeframe.H1, liquidate_end=liquidate_end),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id=strategy.id,
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )


class TwoPairScript(PortfolioStrategyBase):
    id = "two-pair-script"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.n = 0
        self.seen: list[set[Pair]] = []
        self.pos_sizes: list[dict[str, float]] = []

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        self.n += 1
        self.seen.append(set(ctx.candles))
        self.pos_sizes.append({str(p): pos.size for p, pos in ctx.positions().items()})
        if self.n == 2:
            return [OrderIntent(pair=BTC, side=Side.BUY, size=1.0, reason="btc entry")]
        if self.n == 3:
            return [OrderIntent(pair=SOL, side=Side.BUY, size=2.0, reason="sol entry")]
        if self.n == 7:
            return [OrderIntent(pair=BTC, side=Side.SELL, size=1.0, reason="btc exit")]
        if self.n == 8:
            return [OrderIntent(pair=SOL, side=Side.SELL, size=2.0, reason="sol exit")]
        return []


async def test_two_pairs_exact_fills_and_equity() -> None:
    recorder = InMemoryRecorder()
    strategy = TwoPairScript(TwoPairScript.params_schema())
    engine = build_engine(recorder, strategy)
    steps = steps_of({BTC: series(BTC, 100, 10), SOL: series(SOL, 50, 10)})
    result = await engine.run(aiter(steps))

    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 4

    btc_buy, sol_buy, btc_sell, sol_sell = recorder.fills
    # intent on the n-th call (step n-1) fills at the next step's open; prices rise 1/step
    assert (btc_buy.pair, btc_buy.price, btc_buy.ts) == (BTC, 102.0, BASE + timedelta(hours=2))
    assert (sol_buy.pair, sol_buy.price, sol_buy.ts) == (SOL, 53.0, BASE + timedelta(hours=3))
    assert (btc_sell.pair, btc_sell.price, btc_sell.ts) == (BTC, 107.0, BASE + timedelta(hours=7))
    assert (sol_sell.pair, sol_sell.price, sol_sell.ts) == (SOL, 58.0, BASE + timedelta(hours=8))

    # zero fees: 10_000 - 102 - 2*53 + 107 + 2*58
    assert float(result.final_equity) == pytest.approx(10_015.0)
    # one equity snapshot per step; the strategy saw both pairs each step
    assert len(recorder.equity) == 10
    assert strategy.seen == [{BTC, SOL}] * 10
    # positions() exposes per-pair sizes as fills land
    assert strategy.pos_sizes[2] == {"BTC/EUR": 1.0}
    assert strategy.pos_sizes[3] == {"BTC/EUR": 1.0, "SOL/EUR": 2.0}


async def test_missing_candle_step_uses_stale_close_for_equity() -> None:
    class BuySolAt1(TwoPairScript):
        id = "buy-sol-at-1"

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            self.n += 1
            self.seen.append(set(ctx.candles))
            if self.n == 1:
                return [OrderIntent(pair=SOL, side=Side.BUY, size=1.0)]
            return []

    recorder = InMemoryRecorder()
    strategy = BuySolAt1(BuySolAt1.params_schema())
    engine = build_engine(recorder, strategy)
    # SOL has no candle at hour 2; prices rise 1 per candle on both pairs
    steps = steps_of({BTC: series(BTC, 100, 6), SOL: series(SOL, 50, 6, skip={2})})
    result = await engine.run(aiter(steps))

    assert result.status is RunStatus.COMPLETED
    # buy fills at hour-1 open 51; cash is 9_949 afterwards
    assert [(f.pair, f.price) for f in recorder.fills] == [(SOL, 51.0)]
    # hour 2 has only a BTC candle; the SOL position values at the stale close (51)
    assert strategy.seen[2] == {BTC}
    equity_by_ts = {ts: float(eq) for ts, eq, _, _ in recorder.equity}
    assert equity_by_ts[BASE + timedelta(hours=1)] == pytest.approx(10_000.0)
    assert equity_by_ts[BASE + timedelta(hours=2)] == pytest.approx(10_000.0)  # stale carry
    assert equity_by_ts[BASE + timedelta(hours=3)] == pytest.approx(10_002.0)  # SOL close 53
    assert len(recorder.equity) == 6  # one snapshot per step, not per candle
    # the per-pair histories advance only when the pair has a candle
    assert len(engine.history[BTC]) == 6
    assert len(engine.history[SOL]) == 5


async def test_foreign_pair_intent_is_rejected_explicitly() -> None:
    eth = Pair.parse("ETH/EUR")

    class ForeignBuyer(PortfolioStrategyBase):
        id = "foreign-buyer"

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            return [OrderIntent(pair=eth, side=Side.BUY, size=1.0)]

    risk = RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000))
    recorder = InMemoryRecorder()
    engine = build_engine(recorder, ForeignBuyer(ForeignBuyer.params_schema()), risk=risk)
    steps = steps_of({BTC: series(BTC, 100, 5), SOL: series(SOL, 50, 5)})
    result = await engine.run(aiter(steps))

    assert result.status is RunStatus.COMPLETED
    assert recorder.orders == []  # the intent never reaches the venue
    assert recorder.fills == []
    assert any("foreign pair ETH/EUR" in r for r in risk.rejections)


async def test_liquidate_end_closes_every_pair_at_its_last_candle() -> None:
    class BuyBothAt1(PortfolioStrategyBase):
        id = "buy-both-at-1"

        def __init__(self, params):  # type: ignore[no-untyped-def]
            super().__init__(params)
            self.n = 0

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            self.n += 1
            if self.n == 1:
                return [
                    OrderIntent(pair=BTC, side=Side.BUY, size=1.0),
                    OrderIntent(pair=SOL, side=Side.BUY, size=2.0),
                ]
            return []

    recorder = InMemoryRecorder()
    engine = build_engine(recorder, BuyBothAt1(BuyBothAt1.params_schema()), liquidate_end=True)
    steps = steps_of({BTC: series(BTC, 100, 5), SOL: series(SOL, 50, 5)})
    result = await engine.run(aiter(steps))

    assert result.status is RunStatus.COMPLETED
    assert result.num_fills == 4
    btc_buy, sol_buy, btc_liq, sol_liq = recorder.fills
    assert (btc_buy.price, sol_buy.price) == (101.0, 51.0)  # hour-1 opens
    # liquidation sells at the last close (hour 4: 104 / 54)
    assert btc_liq.price == 104.0 and btc_liq.ts == BASE + timedelta(hours=4)
    assert sol_liq.price == 54.0 and sol_liq.ts == BASE + timedelta(hours=4)
    assert float(result.final_equity) == pytest.approx(10_000 - 101 - 102 + 104 + 108)


async def test_daily_loss_rail_halts_on_portfolio_floor_equity() -> None:
    class BuyBtcAt2(PortfolioStrategyBase):
        id = "buy-btc-at-2"

        def __init__(self, params):  # type: ignore[no-untyped-def]
            super().__init__(params)
            self.n = 0

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            self.n += 1
            if self.n == 2:
                return [OrderIntent(pair=BTC, side=Side.BUY, size=1.0)]
            return []

    risk = RiskManager(
        RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000, max_daily_loss_quote=1.0)
    )
    recorder = InMemoryRecorder()
    engine = build_engine(recorder, BuyBtcAt2(BuyBtcAt2.params_schema()), risk=risk)
    falling_btc = [candle(BTC, i, 100 - i) for i in range(10)]
    falling_sol = [candle(SOL, i, 50 - i) for i in range(10)]
    result = await engine.run(aiter(steps_of({BTC: falling_btc, SOL: falling_sol})))

    assert result.status is RunStatus.HALTED
    assert "max daily loss" in result.halt_reason


async def test_warmup_steps_only_populate_history_and_prices() -> None:
    class Observer(PortfolioStrategyBase):
        id = "observer"

        def __init__(self, params):  # type: ignore[no-untyped-def]
            super().__init__(params)
            self.calls: list[int] = []

        def on_candle(self, ctx):  # type: ignore[no-untyped-def]
            self.calls.append(len(ctx.history(BTC, 100)))
            return []

    recorder = InMemoryRecorder()
    strategy = Observer(Observer.params_schema())
    engine = build_engine(recorder, strategy)
    steps = steps_of({BTC: series(BTC, 100, 6), SOL: series(SOL, 50, 6)})
    result = await engine.run(aiter(steps), warmup=2)

    assert result.status is RunStatus.COMPLETED
    assert strategy.calls == [3, 4, 5, 6]  # strategy starts after 2 warmup steps
    assert len(recorder.equity) == 4  # no snapshots during warmup
    # warmup closes count as last known prices: first snapshot sees them
    assert float(recorder.equity[0][1]) == pytest.approx(10_000.0)
