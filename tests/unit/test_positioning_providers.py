"""Positioning providers: point-in-time slicing over canned series (no DB)."""

from datetime import UTC, date, datetime, timedelta

from kaupo.core.positioning import (
    EmptyFuturesMetricsProvider,
    EmptyOpenInterestProvider,
    StaticFuturesMetricsProvider,
    StaticOpenInterestProvider,
)
from kaupo.domain import FuturesMetricsDaily, OpenInterest

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY = date(2026, 1, 1)


def oi(hours: float, value: float = 100.0, base: str = "BTC") -> OpenInterest:
    return OpenInterest(
        exchange="binance",
        base_asset=base,
        ts=BASE + timedelta(hours=hours),
        oi_base=value,
        oi_quote=value * 50_000.0,
    )


def metrics(days: int, value: float = 100.0, base: str = "BTC") -> FuturesMetricsDaily:
    return FuturesMetricsDaily(
        exchange="binance",
        base_asset=base,
        day=DAY + timedelta(days=days),
        oi_base=value,
        oi_quote=value * 50_000.0,
        count_toptrader_ls_ratio=2.0,
        sum_toptrader_ls_ratio=1.5,
        count_ls_ratio=2.5,
        taker_ls_vol_ratio=1.1,
    )


OI_POINTS = [oi(2, 100.0), oi(5.5, 101.0), oi(9.5, 102.0), oi(20, 103.0)]
METRIC_ROWS = [metrics(0, 100.0), metrics(1, 101.0), metrics(2, 102.0), metrics(3, 103.0)]


class TestStaticOpenInterestProvider:
    async def test_point_in_time_and_ascending(self) -> None:
        provider = StaticOpenInterestProvider({"BTC": OI_POINTS})
        await provider.update("BTC", BASE + timedelta(hours=1))  # no-op

        assert provider.latest("BTC", 10, BASE + timedelta(hours=1)) == []
        # boundary is inclusive: a snapshot exactly at now is visible
        assert [r.ts for r in provider.latest("BTC", 10, BASE + timedelta(hours=2))] == [
            BASE + timedelta(hours=2)
        ]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=5)) == OI_POINTS[:1]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=6)) == OI_POINTS[:2]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=100)) == OI_POINTS

    async def test_n_slices_the_newest(self) -> None:
        provider = StaticOpenInterestProvider({"BTC": OI_POINTS})
        now = BASE + timedelta(hours=10)
        assert provider.latest("BTC", 1, now) == OI_POINTS[2:3]
        assert provider.latest("BTC", 2, now) == OI_POINTS[1:3]
        assert provider.latest("BTC", 0, now) == []

    async def test_unknown_base_and_case_and_unsorted(self) -> None:
        provider = StaticOpenInterestProvider({"BTC": [OI_POINTS[2], OI_POINTS[0], OI_POINTS[1]]})
        now = BASE + timedelta(hours=10)
        assert provider.latest("ETH", 10, now) == []
        assert [r.ts for r in provider.latest("btc", 10, now)] == [p.ts for p in OI_POINTS[:3]]


class TestStaticFuturesMetricsProvider:
    async def test_only_fully_closed_days_are_served(self) -> None:
        provider = StaticFuturesMetricsProvider({"BTC": METRIC_ROWS})
        await provider.update("BTC", BASE)  # no-op

        # day 1 (2026-01-01) is in progress all through that day
        assert provider.latest("BTC", 10, BASE) == []
        assert provider.latest("BTC", 10, BASE + timedelta(hours=23)) == []
        # day 1 closes at midnight: its row appears from the first moment of day 2
        assert [r.day for r in provider.latest("BTC", 10, BASE + timedelta(days=1))] == [DAY]
        assert [r.day for r in provider.latest("BTC", 10, BASE + timedelta(days=3))] == [
            DAY,
            DAY + timedelta(days=1),
            DAY + timedelta(days=2),
        ]

    async def test_n_slices_the_newest(self) -> None:
        provider = StaticFuturesMetricsProvider({"BTC": METRIC_ROWS})
        now = BASE + timedelta(days=4)
        assert provider.latest("BTC", 2, now) == METRIC_ROWS[2:]
        assert provider.latest("BTC", 0, now) == []

    async def test_unknown_base_and_case(self) -> None:
        provider = StaticFuturesMetricsProvider({"BTC": METRIC_ROWS})
        now = BASE + timedelta(days=1)
        assert provider.latest("ETH", 10, now) == []
        assert provider.latest("btc", 10, now) == METRIC_ROWS[:1]


class TestEmptyProviders:
    async def test_always_empty(self) -> None:
        oi_provider = EmptyOpenInterestProvider()
        metrics_provider = EmptyFuturesMetricsProvider()
        now = BASE + timedelta(hours=100)
        await oi_provider.update("BTC", now)
        await metrics_provider.update("BTC", now)
        assert oi_provider.latest("BTC", 10, now) == []
        assert metrics_provider.latest("BTC", 10, now) == []
