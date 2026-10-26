"""Real xStocks HTTP mapping through shadow warm-up, polling, and persistence."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest, run_portfolio_shadow, run_shadow
from kaupo.data.candles import get_candles
from kaupo.data.xstocks import XStocksClient
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Pair, Timeframe
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("portfolio", [False, True])
async def test_xstocks_shadow_refresh_and_poll(session, tmp_path, portfolio):
    pairs = [Pair.parse("AAPLX/USD"), Pair.parse("SPYX/USD")] if portfolio else [Pair.parse("AAPLX/USD")]
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    base = "PortfolioStrategyBase" if portfolio else "StrategyBase"
    (tmp_path / "noop.py").write_text(
        f"from kaupo.sdk.protocol import {base}\n"
        f"class Noop({base}):\n"
        '    id = "noop"\n'
        "    def on_candle(self, ctx):\n"
        "        return []\n"
    )
    strategy = load_strategies(tmp_path)["noop"]
    stop = asyncio.Event()
    calls = Counter()
    scheduled = False

    def handler(request):
        nonlocal scheduled
        if request.url.path.endswith("AssetPairs"):
            assert request.url.params["aclass_base"] == "tokenized_asset"
            result = {
                str(p).replace("/", ""): {
                    "aclass_base": "tokenized_asset",
                    "altname": str(p).replace("/", ""),
                    "wsname": str(p),
                    "status": "online",
                    "tick_size": "0.01",
                    "execution_venue": "international",
                }
                for p in pairs
            }
        else:
            assert request.url.path.endswith("OHLC")
            assert request.url.params["asset_class"] == "tokenized_asset"
            symbol = request.url.params["pair"]
            calls[symbol] += 1
            # Warm-up precedes a weekend-sized gap. The next poll delivers one bar.
            offsets = range(65, 116) if calls[symbol] == 1 else [0]
            rows = [
                [int((now - timedelta(hours=i)).timestamp()), "100", "101", "99", "100", "100", "1", 1]
                for i in reversed(list(offsets))
            ]
            result = {symbol: rows, "last": int(now.timestamp())}
            if all(calls[str(p).replace("/", "")] >= 2 for p in pairs) and not scheduled:
                scheduled = True
                asyncio.get_running_loop().call_later(0.5, stop.set)
        return httpx.Response(200, json={"error": [], "result": result})

    kwargs = dict(
        strategy=strategy,
        params={},
        timeframe=Timeframe.H1,
        warmup=50,
        poll_interval_seconds=0.01,
        asset_class="tokenized_asset",
    )
    async with XStocksClient(transport=httpx.MockTransport(handler), request_interval=0) as client:
        if portfolio:
            result = await asyncio.wait_for(
                run_portfolio_shadow(
                    PortfolioShadowRequest(pairs=pairs, **kwargs), get_sessionmaker(), client, stop
                ),
                10,
            )
        else:
            result = await asyncio.wait_for(
                run_shadow(ShadowRequest(pair=pairs[0], **kwargs), get_sessionmaker(), client, stop), 10
            )
    assert result.status.value in ("completed", "halted")
    run = (await session.execute(select(RunRow))).scalar_one()
    assert run.config["asset_class"] == "tokenized_asset"
    snapshots = (await session.execute(select(EquitySnapshotRow))).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].ts == now
    for pair in pairs:
        candles = await get_candles(
            session, pair, Timeframe.H1, now - timedelta(hours=64), now + timedelta(hours=1)
        )
        assert [c.ts for c in candles] == [now]
