"""Refresh persists public candles and reports coverage independently per target."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from kaupo.data.candles import get_candles
from kaupo.data.xstocks import XStocksClient
from kaupo.data.xstocks_refresh import RefreshConfig, refresh_xstocks
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Pair, Timeframe
from tests.unit.test_xstocks_refresh import FRIDAY, schedule

pytestmark = pytest.mark.integration


def manifest(days=1):
    return RefreshConfig.model_validate(
        {
            "days": days,
            "instruments": [
                {
                    "pair": f"{symbol}/USD",
                    "timeframes": ["1h"],
                    "trading_schedule": schedule().model_dump(mode="json"),
                }
                for symbol in ("AAPLX", "SPYX")
            ],
        }
    )


def client_for(*, now, failing=False, empty=False, missing=False):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("AssetPairs"):
            assert request.url.params["aclass_base"] == "tokenized_asset"
            result = {
                f"{symbol}USD": {
                    "aclass_base": "tokenized_asset",
                    "altname": f"{symbol}USD",
                    "wsname": f"{symbol}/USD",
                    "status": "online",
                    "tick_size": "0.01",
                    "execution_venue": "international",
                }
                for symbol in ("AAPLX", "SPYX")
            }
        else:
            assert request.url.path.endswith("OHLC")
            assert request.url.params["asset_class"] == "tokenized_asset"
            pair = request.url.params["pair"]
            if failing and pair == "AAPLXUSD":
                raise httpx.ReadTimeout("source unavailable")
            hours = [] if empty else range(1 if missing else 0, 24)
            rows = [
                [int((FRIDAY + timedelta(hours=i)).timestamp()), "100", "101", "99", "100", "100", "1", 1]
                for i in hours
            ]
            result = {pair: rows, "last": int(now.timestamp())}
        return httpx.Response(200, json={"error": [], "result": result})

    return XStocksClient(
        transport=httpx.MockTransport(handler), now=lambda: now, request_interval=0
    ), requests


async def test_refresh_uses_stored_history_and_is_idempotent(session):
    now = FRIDAY + timedelta(days=1, seconds=2)
    for empty in (False, True):
        client, requests = client_for(now=now, empty=empty)
        async with client:
            results = await refresh_xstocks(manifest(), get_sessionmaker(), client, now=now)
        assert all(result.ok for result in results)
        assert all(result.coverage.expected == result.coverage.observed == 24 for result in results)
        assert all(result.imported == (0 if empty else 24) for result in results)
        assert len(requests) == 3
    stored = await get_candles(session, Pair.parse("AAPLX/USD"), Timeframe.H1, FRIDAY, now)
    assert len(stored) == 24
    assert all(c.volume == 1 for c in stored)
    client, _ = client_for(now=now, failing=True)
    async with client:
        results = await refresh_xstocks(manifest(), get_sessionmaker(), client, now=now)
    assert not results[0].ok  # cached candles cannot hide this fetch failure
    assert "source unavailable" in results[0].error
    assert results[1].ok  # failure must not prevent the next instrument refresh


async def test_refresh_reports_missing_open_bucket(session):
    now = FRIDAY + timedelta(days=1, seconds=2)
    client, _ = client_for(now=now, missing=True)
    async with client:
        results = await refresh_xstocks(manifest(), get_sessionmaker(), client, now=now)
    for result in results:
        assert not result.ok
        assert result.coverage.status == "missing"
        assert result.coverage.missing_sample == [FRIDAY]
        assert result.coverage.observed == 23


@pytest.mark.parametrize("failing", [False, True])
async def test_closed_window_still_fetches_and_reports_errors(session, failing):
    now = datetime(2026, 10, 26, 0, 0, 2, tzinfo=UTC)
    client, requests = client_for(now=now, empty=True, failing=failing)
    async with client:
        results = await refresh_xstocks(manifest(days=2), get_sessionmaker(), client, now=now)
    assert len(requests) == 3
    assert results[0].ok is not failing
    assert results[1].coverage.status == "scheduled_closed"
    assert results[1].coverage.expected == 0
    assert results[1].ok
