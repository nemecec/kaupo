"""Public xStocks API mapping and complete-candle reconstruction."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from kaupo.data.xstocks import XStocksClient, epoch_seconds
from kaupo.domain import Pair, Timeframe

START = datetime(2026, 1, 2, 20, tzinfo=UTC)  # Friday
NOW = START + timedelta(days=4)
PAIR = Pair.parse("AAPLx/USD")


def markets():
    row = {
        "aclass_base": "tokenized_asset",
        "altname": "AAPLxUSD",
        "wsname": "AAPLx/USD",
        "status": "online",
        "tick_size": "0.01",
        "execution_venue": "international",
    }
    return {"AAPLSPVUSD": row, "AAPLxUSD": row.copy()}


def trade(id, hours=0, price="100", size="1", seconds=0):
    return [
        price,
        size,
        str(epoch_seconds(START) + Decimal(hours) * 3600 + Decimal(str(seconds))),
        "b",
        "l",
        "",
        id,
    ]


def make_client(pages, *, now=NOW, endpoint="Trades", market_rows=None):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("AssetPairs"):
            return httpx.Response(
                200, json={"error": [], "result": markets() if market_rows is None else market_rows}
            )
        assert request.url.path.endswith(endpoint)
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, httpx.Response):
            return page
        return httpx.Response(200, json={"error": [], "result": page})

    return XStocksClient(
        transport=httpx.MockTransport(handler), now=lambda: now, request_interval=0
    ), requests


async def collect(client, start=START, end=NOW):
    return [c async for c in client.historical_candles(PAIR, Timeframe.H1, start, end)]


async def test_discovery_deduplicates_aliases_and_uses_correct_filters():
    client, requests = make_client([])
    async with client:
        result = await client.markets("ee")
    assert len(result) == 1
    assert result[0].pair == PAIR
    assert result[0].api_pair == "AAPLxUSD"
    assert dict(requests[0].url.params) == {
        "aclass_base": "tokenized_asset",
        "country_code": "EE",
        "execution_venue": "international",
    }


async def test_unknown_and_ambiguous_market_fail():
    client, _ = make_client([], market_rows={})
    async with client:
        with pytest.raises(ValueError, match="not in Kraken"):
            await client.recent_candles(PAIR, Timeframe.H1)
    rows = markets()
    rows["AAPLSPVUSD"]["altname"] = "different"
    client, _ = make_client([], market_rows=rows)
    async with client:
        with pytest.raises(ValueError, match="Conflicting"):
            await client.markets()


async def test_overlapping_pages_preserve_identical_distinct_trades_and_weekend_gap():
    a = trade(1, price="100", size="0.1", seconds=10)
    b = trade(2, price="102", size="0.2", seconds=20)
    c = trade(3, price="102", size="0.2", seconds=20)  # same tick values, distinct ID
    d = trade(4, hours=64, price="90", size="0.5")  # market reopens Monday
    pages = [
        {"AAPLxUSD": [a, b], "last": "1767384020000000000"},
        {"AAPLxUSD": [b, c, d], "last": "1767614400000000000"},
        {"AAPLxUSD": [d], "last": "1767614400000000000"},
    ]
    client, requests = make_client(pages)
    async with client:
        candles = await collect(client)
    assert len(candles) == 2  # no artificial weekend bars
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close, candles[0].volume) == (
        100,
        102,
        100,
        102,
        0.5,
    )
    assert candles[1].ts == START + timedelta(hours=64)
    assert candles[1].volume == 0.5
    assert str(candles[0].pair) == "AAPLX/USD"
    for request in requests[1:]:
        assert request.url.params["pair"] == "AAPLxUSD"
        assert request.url.params["asset_class"] == "tokenized_asset"
    assert requests[2].url.params["since"] == "1767384020000000000"


async def test_partial_start_end_and_current_buckets_are_excluded():
    page = {"AAPLxUSD": [trade(i + 1, hours=i) for i in range(4)], "last": "1767394800000000000"}
    client, requests = make_client([page], now=START + timedelta(hours=3, seconds=1))
    async with client:
        candles = await collect(client, start=START + timedelta(minutes=15), end=START + timedelta(hours=4))
    assert [c.ts for c in candles] == [START + timedelta(hours=1)]
    assert requests[1].url.params["since"] == str(
        int(epoch_seconds(START + timedelta(hours=1)) * 1_000_000_000) - 1
    )


async def test_page_failure_never_yields_unfinished_bucket():
    client, _ = make_client(
        [
            {"AAPLxUSD": [trade(1)], "last": str(int(epoch_seconds(START) * 1_000_000_000 + 1))},
            httpx.ReadTimeout("failed"),
        ]
    )
    result = []
    async with client:
        with pytest.raises(httpx.ReadTimeout):
            async for candle in client.historical_candles(PAIR, Timeframe.H1, START, NOW):
                result.append(candle)
    assert result == []


async def test_stalled_cursor_with_new_trades_fails():
    cursor = str(int(epoch_seconds(START) * 1_000_000_000) - 1)
    client, _ = make_client([{"AAPLxUSD": [trade(1)], "last": cursor}])
    async with client:
        with pytest.raises(ValueError, match="did not advance"):
            await collect(client)


async def test_changed_overlap_and_out_of_order_data_fail():
    cursor = str(int(epoch_seconds(START) * 1_000_000_000 + 1))
    client, _ = make_client(
        [
            {"AAPLxUSD": [trade(1)], "last": cursor},
            {"AAPLxUSD": [trade(1, price="101")], "last": cursor},
        ]
    )
    async with client:
        with pytest.raises(ValueError, match="changed a trade"):
            await collect(client)
    client, _ = make_client([{"AAPLxUSD": [trade(2, hours=1), trade(1)], "last": cursor}])
    async with client:
        with pytest.raises(ValueError, match="not in timestamp"):
            await collect(client)


async def test_recent_ohlc_drops_unclosed_bars_and_uses_asset_class():
    ts = int(START.timestamp())
    rows = [
        [ts - 3600, "100", "100", "100", "100", "0", "0", 0],
        [ts, "100", "103", "99", "102", "101", "2", 3],
        [ts + 3600, "102", "104", "100", "103", "102", "1", 2],
    ]
    client, requests = make_client(
        [{"AAPLxUSD": rows, "last": ts}], endpoint="OHLC", now=START + timedelta(hours=1, seconds=2)
    )
    async with client:
        candles = await client.recent_candles(PAIR, Timeframe.H1)
    assert len(candles) == 1
    assert candles[0].volume == 2
    assert dict(requests[-1].url.params) == {
        "pair": "AAPLxUSD",
        "asset_class": "tokenized_asset",
        "interval": "60",
    }


async def test_api_errors_and_malformed_trade_fail():
    error = httpx.Response(200, json={"error": ["EGeneral:Invalid arguments"]})
    client, _ = make_client([error])
    async with client:
        with pytest.raises(ValueError, match="Kraken Trades"):
            await collect(client)
    client, _ = make_client([{"AAPLxUSD": [trade(1, price="NaN")], "last": "1"}])
    async with client:
        with pytest.raises(ValueError, match="Invalid xStocks trade"):
            await collect(client)


async def test_fractional_timestamp_keeps_trade_before_hour_boundary():
    # Parse JSON numbers as Decimal: binary float would round this to the next hour.
    timestamp = str(epoch_seconds(START + timedelta(hours=1)) - Decimal("0.000000001"))
    payload = (
        '{"error":[],"result":{"AAPLxUSD":[["100","1",'
        + timestamp
        + ',"b","l","",1]],"last":"1767387600000000000"}}'
    )
    client, _ = make_client(
        [httpx.Response(200, text=payload), {"AAPLxUSD": [], "last": "1767387600000000000"}]
    )
    async with client:
        candles = await collect(client)
    assert candles[0].ts == START


async def test_invalid_or_empty_ranges_do_not_fetch():
    client, requests = make_client([])
    async with client:
        with pytest.raises(ValueError, match="start must be before"):
            await collect(client, end=START)
        assert await collect(client, end=START + timedelta(minutes=30)) == []
    assert requests == []


async def test_polling_adapter_filters_since_and_limit_without_filling_closures():
    ts = int(START.timestamp())
    rows = [[ts + h * 3600, "100", "103", "99", "102", "101", "2", 3] for h in (0, 1, 64)]
    client, requests = make_client([{"AAPLxUSD": rows, "last": ts}] * 2, endpoint="OHLC")
    async with client:
        candles = await client.fetch_candles(PAIR, Timeframe.H1, since=START + timedelta(hours=1))
        latest = await client.fetch_candles(PAIR, Timeframe.H1, limit=1)
    assert [c.ts for c in candles] == [START + timedelta(hours=h) for h in (1, 64)]
    assert latest == candles[-1:]
    assert all(r.url.params["asset_class"] == "tokenized_asset" for r in requests[1:])


async def test_shadow_preflight_validates_entire_universe_and_client():
    from kaupo.core.runner import _validate_shadow_source

    client, requests = make_client([])
    async with client:
        await _validate_shadow_source("tokenized_asset", client, [PAIR], None)
        with pytest.raises(ValueError, match="not in Kraken"):
            await _validate_shadow_source("tokenized_asset", client, [PAIR, Pair.parse("BTC/USD")], None)
        with pytest.raises(ValueError, match="does not match"):
            await _validate_shadow_source("currency", client, [PAIR], None)
        with pytest.raises(ValueError, match="funding"):
            await _validate_shadow_source("tokenized_asset", client, [PAIR], object())
    assert len(requests) == 1  # discovery cached; no OHLC or funding requests


@pytest.mark.parametrize("portfolio", [False, True])
async def test_unknown_market_stops_shadow_before_database_access(portfolio):
    from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest, run_portfolio_shadow, run_shadow

    client, requests = make_client([])
    kwargs = dict(strategy=object(), params={}, timeframe=Timeframe.H1, asset_class="tokenized_asset")
    async with client:
        with pytest.raises(ValueError, match="not in Kraken"):
            if portfolio:
                await run_portfolio_shadow(
                    PortfolioShadowRequest(pairs=[PAIR, Pair.parse("BTC/USD")], **kwargs), None, client
                )
            else:
                await run_shadow(ShadowRequest(pair=Pair.parse("BTC/USD"), **kwargs), None, client)
    assert len(requests) == 1


async def test_raw_trade_stream_keeps_sides_ids_and_half_open_bounds():
    first = trade(1, seconds=0.1)
    second = trade(2, seconds=0.1)
    second[3] = "s"
    before = trade(0, seconds=0)
    end = trade(3, hours=1)
    client, requests = make_client(
        [
            {"AAPLxUSD": [before, first], "last": "1767384000100000000"},
            {"AAPLxUSD": [first, second, end], "last": "1767387600000000000"},
        ]
    )
    async with client:
        trades = [
            t
            async for t in client.historical_trades(
                PAIR, START + timedelta(microseconds=100000), START + timedelta(hours=1)
            )
        ]
    assert [t.id for t in trades] == [1, 2]
    assert [t.side for t in trades] == ["buy", "sell"]
    assert requests[1].url.params["since"] == "1767384000099999999"


async def test_changed_side_across_overlap_is_rejected():
    first = trade(1)
    changed = trade(1)
    changed[3] = "s"
    client, _ = make_client(
        [
            {"AAPLxUSD": [first], "last": "1767384000000000001"},
            {"AAPLxUSD": [changed], "last": "1767384000000000001"},
        ]
    )
    async with client:
        with pytest.raises(ValueError, match="changed a trade"):
            _ = [t async for t in client.historical_trades(PAIR, START, NOW)]


async def test_raw_trade_empty_range_fails_before_fetch():
    client, requests = make_client([])
    async with client:
        with pytest.raises(ValueError, match="start must be before"):
            _ = [t async for t in client.historical_trades(PAIR, START, START)]
    assert not requests


async def test_discovery_retains_order_rules_without_inventing_missing_values():
    rows = markets()
    for row in rows.values():
        row.update(ordermin="0.01", costmin="0.5", lot_decimals=8, lot_multiplier=1, lot="unit")
    client, _ = make_client([], market_rows=rows)
    async with client:
        (market,) = await client.markets()
    assert (market.order_min, market.cost_min, market.lot_decimals, market.lot_multiplier, market.lot) == (
        "0.01",
        "0.5",
        8,
        1,
        "unit",
    )
    client, _ = make_client([])
    async with client:
        (market,) = await client.markets()
    assert market.order_min is None and market.lot_decimals is None
