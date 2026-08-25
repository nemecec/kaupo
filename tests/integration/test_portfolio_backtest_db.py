"""Persisted portfolio backtest over a 3-pair universe (testcontainers)."""

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, FillRow, OrderRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

PAIRS = [Pair.parse(p) for p in ("ADA/EUR", "BTC/EUR", "SOL/EUR")]
BASE = datetime(2026, 1, 1, tzinfo=UTC)

STRATEGY = textwrap.dedent(
    """
    from kaupo.sdk.protocol import PortfolioStrategyBase
    from kaupo.domain import OrderIntent, Pair, Side

    BTC = Pair.parse("BTC/EUR")

    class BuyAndSellBtc(PortfolioStrategyBase):
        id = "portfolio-buy-sell"
        def __init__(self, params):
            super().__init__(params)
            self.n = 0
        def on_candle(self, ctx):
            self.n += 1
            if self.n == 3:
                return [OrderIntent(pair=BTC, side=Side.BUY, size=1.0)]
            if self.n == 7:
                return [OrderIntent(pair=BTC, side=Side.SELL, size=1.0)]
            return []
    """
)


def _candles(pair: Pair, base_price: float, n: int) -> list[Candle]:
    return [
        Candle(
            pair=pair,
            timeframe=Timeframe.H1,
            ts=BASE + timedelta(hours=i),
            open=base_price + i,
            high=base_price + i + 1,
            low=base_price + i - 1,
            close=base_price + i,
            volume=1.0,
        )
        for i in range(n)
    ]


async def test_portfolio_backtest_persists_full_run(session: AsyncSession, tmp_path: Path) -> None:
    for j, pair in enumerate(PAIRS):
        await upsert_candles(session, _candles(pair, 100 * (j + 1), 12))
    await session.commit()

    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["portfolio-buy-sell"]

    run_id, result, metrics = await run_portfolio_backtest(
        PortfolioBacktestRequest(
            strategy=strategy,
            params={},
            # intentionally unsorted: the request canonicalizes the universe
            pairs=[Pair.parse("BTC/EUR"), Pair.parse("SOL/EUR"), Pair.parse("ADA/EUR")],
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=12),
        ),
        get_sessionmaker(),
    )

    assert result.status.value == "completed"
    assert result.num_fills == 2
    assert metrics["num_fills"] == 2
    assert metrics["universe"] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]

    per_pair = metrics["per_pair"]
    assert list(per_pair) == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]
    btc = per_pair["BTC/EUR"]
    assert btc["round_trips"] == 1
    # BTC ramps up 1/candle from 200: buy at ~203, sell at ~207 -> positive pnl
    assert btc["realized_pnl"] > 0
    assert btc["fees_paid"] > 0
    assert btc["win_rate_pct"] == 100.0
    assert per_pair["ADA/EUR"] == {
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "round_trips": 0,
        "win_rate_pct": None,
    }

    run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
    assert run.mode == "backtest"
    assert run.status == "completed"
    assert run.strategy_id == "portfolio-buy-sell"
    # the runs row keeps config["pair"] a plain string: the joined sorted list
    assert run.config["pair"] == "ADA/EUR,BTC/EUR,SOL/EUR"
    assert run.config["pairs"] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]
    assert run.metrics["universe"] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]
    assert run.ended_at is not None

    orders = (await session.execute(select(OrderRow).where(OrderRow.run_id == run_id))).scalars().all()
    assert len(orders) == 2
    assert all(o.pair == "BTC/EUR" and o.status == "filled" for o in orders)

    fills = (await session.execute(select(FillRow).where(FillRow.run_id == run_id))).scalars().all()
    assert len(fills) == 2
    assert [f.side for f in fills] == ["buy", "sell"]

    equity = (
        (await session.execute(select(EquitySnapshotRow).where(EquitySnapshotRow.run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(equity) == 12  # one snapshot per joined step


async def test_portfolio_backtest_without_candles_fails_clearly(
    session: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "s.py").write_text(STRATEGY)
    strategy = load_strategies(tmp_path)["portfolio-buy-sell"]

    with pytest.raises(ValueError, match="No kraken candles for BTC/EUR"):
        await run_portfolio_backtest(
            PortfolioBacktestRequest(
                strategy=strategy,
                params={},
                pairs=[Pair.parse("BTC/EUR"), Pair.parse("SOL/EUR")],
                timeframe=Timeframe.H1,
                start=BASE,
                end=BASE + timedelta(hours=12),
            ),
            get_sessionmaker(),
        )
