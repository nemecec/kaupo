"""Historical xStocks imports persist complete, repeatable candle aggregates."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.data.candles import get_candles
from kaupo.data.xstocks import epoch_seconds, ingest_xstock_candles
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Timeframe
from tests.unit.test_xstocks import NOW, PAIR, START, make_client, trade

pytestmark = pytest.mark.integration


async def test_repeated_history_import_preserves_volume_and_gaps(session: AsyncSession) -> None:
    a, b = trade(1, size="0.1"), trade(2, size="0.1")
    c = trade(3, hours=64, price="90")
    for _ in range(2):
        client, _ = make_client(
            [
                {"AAPLxUSD": [a, b, c], "last": "1767614400000000000"},
                {"AAPLxUSD": [c], "last": "1767614400000000000"},
            ]
        )
        async with client:
            count = await ingest_xstock_candles(client, get_sessionmaker(), PAIR, Timeframe.H1, START, NOW)
        assert count == 2
    candles = await get_candles(session, PAIR, Timeframe.H1, START, NOW)
    assert len(candles) == 2
    assert candles[0].volume == pytest.approx(0.2)
    assert candles[1].ts - candles[0].ts == timedelta(hours=64)
    assert all(c.exchange == "kraken" and str(c.pair) == "AAPLX/USD" for c in candles)


async def test_failure_retains_only_committed_complete_buckets(session: AsyncSession) -> None:
    end = START + timedelta(days=10)
    cursor = str(int(epoch_seconds(START + timedelta(hours=103)) * 1_000_000_000))
    client, _ = make_client(
        [
            {"AAPLxUSD": [trade(i + 1, hours=i) for i in range(103)], "last": cursor},
            httpx.ReadTimeout("upstream failed"),
        ],
        now=end,
    )
    async with client:
        with pytest.raises(httpx.ReadTimeout):
            await ingest_xstock_candles(client, get_sessionmaker(), PAIR, Timeframe.H1, START, end)
    candles = await get_candles(session, PAIR, Timeframe.H1, START, end)
    assert len(candles) == 100
    assert candles[-1].ts == START + timedelta(hours=99)
    assert all(c.volume == 1 for c in candles)


async def test_recent_import_does_not_write_partial_requested_bucket(session: AsyncSession) -> None:
    ts = int(START.timestamp())
    rows = [[ts + i * 3600, "100", "101", "99", "100", "100", "1", 1] for i in range(3)]
    client, _ = make_client([{"AAPLxUSD": rows, "last": ts}], endpoint="OHLC")
    async with client:
        count = await ingest_xstock_candles(
            client,
            get_sessionmaker(),
            PAIR,
            Timeframe.H1,
            START + timedelta(minutes=15),
            START + timedelta(hours=2, minutes=30),
            recent=True,
        )
    assert count == 1
    candles = await get_candles(session, PAIR, Timeframe.H1, START, NOW)
    assert [c.ts for c in candles] == [START + timedelta(hours=1)]
