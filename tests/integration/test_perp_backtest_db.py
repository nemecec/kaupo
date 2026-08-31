"""Perp backtest end to end: shorts, funding cash flows, the liquidation rail."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import upsert_candles
from kaupo.data.funding import upsert_funding_rates
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, FundingRate, Pair, RunStatus, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

STRATEGY = textwrap.dedent(
    """
    from kaupo.domain import OrderIntent, OrderType, Side
    from kaupo.sdk.protocol import StrategyBase

    class ShortFirstCandle(StrategyBase):
        id = "short-first-candle"
        def __init__(self, params):
            super().__init__(params)
        def on_candle(self, ctx):
            if len(ctx.history(300)) == 1:  # decide once, on the first candle
                return [OrderIntent(pair=ctx.candle.pair, side=Side.SELL,
                                    order_type=OrderType.MARKET, size=self.params.size)]
            return []
    """
)


def candles(prices: list[float]) -> list[Candle]:
    return [
        Candle(
            pair=PAIR,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=prices[0] if i == 0 else prices[i - 1],
            high=max(prices[i], prices[i - 1] if i else prices[0]),
            low=min(prices[i], prices[i - 1] if i else prices[0]),
            close=prices[i],
            volume=1.0,
        )
        for i in range(len(prices))
    ]


def write_strategy(tmp_path: Path, size: float) -> None:
    (tmp_path / "s.py").write_text(STRATEGY.replace("self.params.size", str(size)))


async def test_perp_short_with_funding_point_in_time(session: AsyncSession, tmp_path: Path) -> None:
    await upsert_candles(session, candles([100.0] * 20))
    await upsert_funding_rates(
        session,
        [
            FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=8), rate=0.001),
            FundingRate(exchange="binance", base_asset="BTC", ts=BASE + timedelta(hours=16), rate=-0.002),
        ],
    )
    await session.commit()
    write_strategy(tmp_path, 1.0)
    strategy = load_strategies(tmp_path)["short-first-candle"]

    _, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=20),
            instrument="perp",
        ),
        get_sessionmaker(),
    )

    assert result.status == RunStatus.COMPLETED
    # hand-computed: short 1 @ slipped open 99.95 (fee 0.25987); funding
    # +0.10 at candle 8 (short receives on a positive rate) and -0.20 at
    # candle 16; the wind-down covers at slipped close 100.05 (fee 0.26013)
    # cash: 10000 + 99.95 - 0.25987 + 0.10 - 0.20 - 100.05 - 0.26013
    assert float(result.final_equity) == pytest.approx(9999.28, abs=0.01)


async def test_perp_liquidation_rail_halts_the_run(session: AsyncSession, tmp_path: Path) -> None:
    # gap up 2.5x right after the short opens: equity goes below zero
    await upsert_candles(session, candles([100.0, 250.0, 250.0, 250.0]))
    await session.commit()
    write_strategy(tmp_path, 10.0)
    strategy = load_strategies(tmp_path)["short-first-candle"]

    _, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=4),
            starting_cash=1_000.0,
            instrument="perp",
        ),
        get_sessionmaker(),
    )

    assert result.status == RunStatus.HALTED
    assert "liquidated" in result.halt_reason
    # short 10 @ slipped open 99.95 (fee 2.5987): cash 1996.9013;
    # forced cover at close 250 (fee 6.5), forced past zero
    assert float(result.final_equity) == pytest.approx(-509.60, abs=0.01)


async def test_spot_backtest_unchanged_without_instrument(session: AsyncSession, tmp_path: Path) -> None:
    """The same short intent on spot: clamped away, no funding, no rail."""
    await upsert_candles(session, candles([100.0] * 5))
    await session.commit()
    write_strategy(tmp_path, 1.0)
    strategy = load_strategies(tmp_path)["short-first-candle"]

    _, result, _ = await run_backtest(
        BacktestRequest(
            strategy=strategy,
            params={},
            pair=PAIR,
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=5),
        ),
        get_sessionmaker(),
    )

    assert result.status == RunStatus.COMPLETED
    assert result.num_fills == 0  # the spot clamp rejects the short outright
    assert float(result.final_equity) == pytest.approx(10_000.0)
