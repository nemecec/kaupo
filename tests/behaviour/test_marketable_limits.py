"""Behaviour: a reposting exit against the three marketable-limit modes (kaupo#36).

The scenario is the one the ticket measured: a maker strategy posts its exit
at the decision candle's close, and the next candle opens at or through that
price — so the limit was marketable the moment it was posted. In `skip` mode
(what the live venue does) the exit does not happen, the strategy reposts, and
it fills a candle later at a different price. That difference is the whole
point of the fix, so it is pinned here end to end.
"""

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
from kaupo.venues.paper import MarketableLimit, PaperVenue

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

# (open, high, low, close) per candle:
#  0  flat            -> the strategy decides to enter
#  1  flat            -> the market entry fills at 100; exit posted at 100
#  2  opens AT 100    -> the exit limit (100) is marketable at posting
#  3  opens at 90     -> the reposted exit (95) rests, and the range touches it
#  4  flat, for the tail
CANDLES = [
    (100.0, 100.0, 100.0, 100.0),
    (100.0, 100.0, 100.0, 100.0),
    (100.0, 101.0, 99.0, 95.0),
    (90.0, 96.0, 89.0, 95.0),
    (95.0, 95.0, 95.0, 95.0),
]


def candle(i: int) -> Candle:
    o, h, low, c = CANDLES[i]
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
    )


class EnterThenRepostExit(StrategyBase):
    """Buy at market once, then post a limit exit at the close every candle."""

    id = "repost-exit"

    def __init__(self, params):  # type: ignore[no-untyped-def]
        super().__init__(params)
        self.entered = False

    def on_candle(self, ctx):  # type: ignore[no-untyped-def]
        if not self.entered:
            self.entered = True
            return [OrderIntent(pair=PAIR, side=Side.BUY, size=1.0, reason="entry")]
        if ctx.position().size <= 0:
            return []
        return [
            OrderIntent(
                pair=PAIR,
                side=Side.SELL,
                size=ctx.position().size,
                order_type=OrderType.LIMIT,
                limit_price=ctx.candle.close,
                reason="maker exit at close",
            )
        ]


async def aiter(candles: list[Candle]) -> AsyncIterator[Candle]:
    for c in candles:
        yield c


def build_engine(
    recorder: InMemoryRecorder,
    mode: MarketableLimit,
    *,
    taker_fee_bps: float = 0.0,
    maker_fee_bps: float = 0.0,
) -> Engine:
    strategy = EnterThenRepostExit(EnterThenRepostExit.params_schema())
    return Engine(
        strategy=strategy,
        venue=PaperVenue(
            taker_fee_bps,
            maker_fee_bps,
            slippage_bps=0,  # zero slippage for exact math
            marketable_limit=mode,
        ),
        risk=RiskManager(
            RiskConfig(
                max_position_quote=10_000,
                max_gross_exposure_quote=10_000,
                taker_fee_bps=taker_fee_bps,
                slippage_bps=0,
            )
        ),
        ledger=Ledger("EUR", 10_000.0, BASE),
        recorder=recorder,
        config=EngineConfig(pair=PAIR, timeframe=Timeframe.H1),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id="repost-exit",
            strategy_version="v1",
            strategy_source_hash="x",
            config={},
        ),
    )


async def run(mode: MarketableLimit, **fees: float) -> InMemoryRecorder:
    recorder = InMemoryRecorder()
    engine = build_engine(recorder, mode, **fees)
    result = await engine.run(aiter([candle(i) for i in range(len(CANDLES))]))
    assert result.status is RunStatus.COMPLETED
    return recorder


class TestSkipMode:
    async def test_the_marketable_exit_skips_and_a_repost_fills_later(self) -> None:
        recorder = await run("skip")

        buy, sell = recorder.fills
        assert buy.side is Side.BUY
        assert buy.price == 100.0
        assert buy.ts == BASE + timedelta(hours=1)

        # the exit posted on candle 1 at 100 was marketable at candle 2's open
        # and never happened; the repost at 95 filled on candle 3
        assert sell.side is Side.SELL
        assert sell.price == 95.0
        assert sell.ts == BASE + timedelta(hours=3)

    async def test_the_skipped_exit_is_recorded_as_cancelled(self) -> None:
        recorder = await run("skip")

        # an order is recorded at submit and again once it closes, so the
        # audit trail holds it twice; dedupe on the id to see the final state
        sells = {o.id: o for o in recorder.orders if o.side is Side.SELL}
        by_status = {o.status: o for o in sells.values()}
        assert len(sells) == 2
        # the attempt the venue refused is kept, not silently dropped
        assert by_status[OrderStatus.CANCELLED].limit_price == 100.0
        assert by_status[OrderStatus.FILLED].limit_price == 95.0

    async def test_the_position_is_still_open_on_the_skipped_candle(self) -> None:
        recorder = await run("skip")

        # equity per candle: the position survives candle 2 and is marked at
        # its close (95), so the snapshot dips before the exit finally happens
        by_ts = {e[0]: e[1] for e in recorder.equity}
        assert by_ts[BASE + timedelta(hours=2)] == pytest.approx(9_995.0)


class TestMakerAndTakerModes:
    async def test_maker_mode_takes_the_exit_a_candle_earlier(self) -> None:
        """The legacy model: the same scenario exits on the marketable candle."""
        recorder = await run("maker")

        _, sell = recorder.fills
        assert sell.price == 100.0  # the limit was honoured at the open
        assert sell.ts == BASE + timedelta(hours=2)

    async def test_the_two_modes_disagree_by_a_whole_candle_and_5_of_price(self) -> None:
        maker, skip = await run("maker"), await run("skip")

        maker_exit = maker.fills[1]
        skip_exit = skip.fills[1]
        assert maker_exit.price - skip_exit.price == pytest.approx(5.0)
        assert skip_exit.ts - maker_exit.ts == timedelta(hours=1)

    async def test_taker_mode_charges_the_taker_fee_on_the_marketable_exit(self) -> None:
        recorder = await run("taker", taker_fee_bps=26.0, maker_fee_bps=16.0)

        sell = recorder.fills[1]
        assert sell.price == 100.0
        assert sell.ts == BASE + timedelta(hours=2)
        assert sell.fee == pytest.approx(100.0 * 1.0 * 0.0026)

    async def test_maker_mode_charges_the_maker_fee_on_the_same_exit(self) -> None:
        recorder = await run("maker", taker_fee_bps=26.0, maker_fee_bps=16.0)

        sell = recorder.fills[1]
        assert sell.price == 100.0
        assert sell.fee == pytest.approx(100.0 * 1.0 * 0.0016)
        # the ticket's impact in one number: 10 bps of the exit notional
        taker = await run("taker", taker_fee_bps=26.0, maker_fee_bps=16.0)
        assert taker.fills[1].fee - sell.fee == pytest.approx(100.0 * 1.0 * 0.0010)
