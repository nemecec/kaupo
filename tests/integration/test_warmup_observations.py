"""Backtest prefill counts actual observations across closures, without future data."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.data.candles import get_latest_candles, upsert_candles
from kaupo.data.funding import upsert_funding_rates
from kaupo.db.models import EquitySnapshotRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, FundingRate, Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration
START = datetime(2026, 6, 8, tzinfo=UTC)
BTC = Pair.parse("BTC/USD")
SOL = Pair.parse("SOL/USD")


def candle(pair, hour, price=100, exchange="kraken"):
    return Candle(
        pair, Timeframe.H1, START + timedelta(hours=hour), price, price, price, price, 1, exchange=exchange
    )


async def test_latest_before_is_bounded_and_isolated(session):
    history = [candle(BTC, h) for h in [-100, -90, -80, -70, -60, 0, 1]]
    await upsert_candles(
        session,
        [
            *history,
            candle(BTC, -1, exchange="binance"),
            candle(SOL, -1),
            replace(candle(BTC, -1), timeframe=Timeframe.H4),
        ],
    )
    rows = await get_latest_candles(session, BTC, Timeframe.H1, 4, before=START)
    assert [c.ts for c in rows] == [c.ts for c in history[1:5]]
    assert await get_latest_candles(session, BTC, Timeframe.H1, 0, before=START) == []
    assert await get_latest_candles(session, BTC, Timeframe.H1, 4, before=history[0].ts) == []
    rows = await get_latest_candles(session, BTC, Timeframe.H1, 10, before=START, exchange="binance")
    assert len(rows) == 1
    assert rows[0].exchange == "binance"
    # Existing unbounded callers still receive the latest observations.
    rows = await get_latest_candles(session, BTC, Timeframe.H1, 2)
    assert [c.ts for c in rows] == [START, START + timedelta(hours=1)]


SINGLE = """
from kaupo.sdk.protocol import StrategyBase

class Recorder(StrategyBase):
    id = "record-history"
    seen = []
    def on_candle(self, ctx):
        type(self).seen.append({str(ctx.candle.pair): (
            [c.close for c in ctx.history(4)], [r.rate for r in ctx.funding(10)]
        )})
        return []
"""

PORTFOLIO = """
from kaupo.sdk.protocol import PortfolioStrategyBase

class Recorder(PortfolioStrategyBase):
    id = "record-history"
    seen = []
    def on_candle(self, ctx):
        type(self).seen.append({str(pair): (
            [c.close for c in ctx.history(pair, 4)], [r.rate for r in ctx.funding(pair, 10)]
        ) for pair in ctx.candles})
        return []
"""


@pytest.mark.parametrize("portfolio", [False, True])
@pytest.mark.parametrize("exchange", ["kraken", "binance"])
async def test_backtest_counts_prefill_per_pair_and_extends_auxiliary_history(
    session, tmp_path, portfolio, exchange
):
    pairs = [BTC, SOL] if portfolio else [BTC]
    for i, pair in enumerate(pairs):
        hours = [-100, -90, -80, -70, -60] if pair == BTC else [-5, -4, -3, -2, -1]
        history = [candle(pair, h, 101 + j + 100 * i, exchange) for j, h in enumerate(hours)]
        live = [candle(pair, h, 200 + 100 * i, exchange) for h in [0, 1, 2]]
        await upsert_candles(session, history + live + [candle(pair, 3, 999, exchange)])
        await upsert_candles(session, [candle(pair, -1, 999, "other")])
        await upsert_funding_rates(
            session,
            [
                FundingRate("binance", pair.base, history[1].ts, 0.0001),
                FundingRate("binance", pair.base, START + timedelta(hours=3), 0.999),
            ],
        )
    await session.commit()
    (tmp_path / "s.py").write_text(PORTFOLIO if portfolio else SINGLE)
    strategy = load_strategies(tmp_path)["record-history"]
    common = dict(
        strategy=strategy,
        params={},
        timeframe=Timeframe.H1,
        start=START,
        end=START + timedelta(hours=3),
        lookback=4,
        exchange=exchange,
    )
    if portfolio:
        request = PortfolioBacktestRequest(pairs=pairs, **common)
        run_id, result, _ = await run_portfolio_backtest(request, get_sessionmaker())
    else:
        request = BacktestRequest(pair=BTC, **common)
        run_id, result, _ = await run_backtest(request, get_sessionmaker())
    assert result.num_fills == 0
    assert len(strategy.cls.seen) == 3  # no strategy calls during warm-up
    for i, pair in enumerate(pairs):
        # The engine keeps four observations including the current candle.
        assert strategy.cls.seen[0][str(pair)] == (
            [103 + 100 * i, 104 + 100 * i, 105 + 100 * i, 200 + 100 * i],
            [0.0001],
        )
        assert strategy.cls.seen[1][str(pair)] == (
            [104 + 100 * i, 105 + 100 * i, 200 + 100 * i, 200 + 100 * i],
            [0.0001],
        )
    snapshots = (
        await session.scalars(
            select(EquitySnapshotRow).where(EquitySnapshotRow.run_id == run_id).order_by(EquitySnapshotRow.ts)
        )
    ).all()
    assert [row.ts for row in snapshots] == [START, START + timedelta(hours=1), START + timedelta(hours=2)]
