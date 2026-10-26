"""Historical xStocks imports persist complete, repeatable candle aggregates."""

from datetime import timedelta
from decimal import Decimal

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


async def test_raw_import_preserves_identical_prints_and_orderflow_totals(session):
    from kaupo.data.orderflow_daily import rollup_orderflow_daily
    from kaupo.data.trades import get_trade_ticks, prune_trade_ticks
    from kaupo.data.xstocks import ingest_xstock_trades
    from tests.unit.test_xstocks_refresh import schedule

    a, b, c = trade(1, size="0.1"), trade(2, size="0.1"), trade(3, size="0.2")
    c[3] = "s"
    for _ in range(2):
        client, _ = make_client(
            [
                {"AAPLxUSD": [a, b], "last": "1767384000000000001"},
                {"AAPLxUSD": [a, b, c], "last": "1767384000000000002"},
                {"AAPLxUSD": [c], "last": "1767384000000000002"},
            ]
        )
        async with client:
            assert await ingest_xstock_trades(client, get_sessionmaker(), PAIR, START, NOW) == 3
    ticks = await get_trade_ticks(session, "kraken", str(PAIR), START, NOW)
    assert [tick.trade_id for tick in ticks] == ["1", "2", "3"]
    assert [tick.side for tick in ticks] == ["buy", "buy", "sell"]
    rolled = await rollup_orderflow_daily(
        session, "kraken", str(PAIR), START.date(), trading_schedule=schedule()
    )
    assert rolled.trade_count == 3
    assert rolled.buy_count == 2
    assert rolled.sell_count == 1
    assert rolled.buy_volume == pytest.approx(0.2)
    assert rolled.sell_volume == pytest.approx(0.2)
    assert await prune_trade_ticks(session, "kraken", str(PAIR), START + timedelta(seconds=1)) == 3


async def test_raw_import_floors_nanoseconds_and_keeps_distinct_ids(session):
    from kaupo.data.trades import get_trade_ticks
    from kaupo.data.xstocks import ingest_xstock_trades

    a, b = trade(1), trade(2)
    a[2] = str(epoch_seconds(START + timedelta(hours=1)) - Decimal("0.000000001"))
    b[2] = a[2]
    client, _ = make_client(
        [
            {"AAPLxUSD": [a, b], "last": "1767387600000000000"},
            {"AAPLxUSD": [], "last": "1767387600000000000"},
        ]
    )
    async with client:
        await ingest_xstock_trades(client, get_sessionmaker(), PAIR, START, START + timedelta(hours=1))
    ticks = await get_trade_ticks(session, "kraken", str(PAIR), START, START + timedelta(hours=1))
    assert len(ticks) == 2
    assert all(t.ts == START + timedelta(hours=1, microseconds=-1) for t in ticks)


async def test_raw_import_failure_keeps_committed_batches_for_retry(session):
    from kaupo.data.trades import get_trade_ticks
    from kaupo.data.xstocks import ingest_xstock_trades

    page = {"AAPLxUSD": [trade(i + 1, seconds=i) for i in range(103)], "last": "1767384102000000001"}
    client, _ = make_client([page, httpx.ReadTimeout("failed")])
    async with client:
        with pytest.raises(httpx.ReadTimeout):
            await ingest_xstock_trades(client, get_sessionmaker(), PAIR, START, NOW)
    assert len(await get_trade_ticks(session, "kraken", str(PAIR), START, NOW)) == 100
    client, _ = make_client([page, {"AAPLxUSD": [], "last": page["last"]}])
    async with client:
        await ingest_xstock_trades(client, get_sessionmaker(), PAIR, START, NOW)
    assert len(await get_trade_ticks(session, "kraken", str(PAIR), START, NOW)) == 103
