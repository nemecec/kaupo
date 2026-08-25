"""Funding providers: point-in-time slicing over canned series (no DB)."""

from datetime import UTC, datetime, timedelta

from kaupo.core.funding import DbFundingProvider, EmptyFundingProvider, StaticFundingProvider
from kaupo.domain import FundingRate

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def rate(hours: float, value: float = 0.0001, base: str = "BTC") -> FundingRate:
    return FundingRate(exchange="binance", base_asset=base, ts=BASE + timedelta(hours=hours), rate=value)


# points straddling the "candle close" grid: 2h lands on a close, 5.5h between
POINTS = [rate(2, 0.0001), rate(5.5, -0.0002), rate(9.5, 0.0003), rate(20, 0.0004)]


class TestStaticFundingProvider:
    async def test_point_in_time_and_ascending(self) -> None:
        provider = StaticFundingProvider({"BTC": POINTS})
        now = BASE + timedelta(hours=1)
        assert await provider.update("BTC", now) is None  # no-op

        assert provider.latest("BTC", 10, BASE + timedelta(hours=1)) == []
        # boundary is inclusive: a point exactly at now is visible
        assert [r.ts for r in provider.latest("BTC", 10, BASE + timedelta(hours=2))] == [
            BASE + timedelta(hours=2)
        ]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=5)) == POINTS[:1]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=6)) == POINTS[:2]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=10)) == POINTS[:3]
        assert provider.latest("BTC", 10, BASE + timedelta(hours=100)) == POINTS

    async def test_n_slices_the_newest(self) -> None:
        provider = StaticFundingProvider({"BTC": POINTS})
        now = BASE + timedelta(hours=10)
        assert provider.latest("BTC", 1, now) == POINTS[2:3]
        assert provider.latest("BTC", 2, now) == POINTS[1:3]
        assert provider.latest("BTC", 0, now) == []
        assert provider.latest("BTC", -1, now) == []

    async def test_unknown_base_and_case(self) -> None:
        provider = StaticFundingProvider({"BTC": POINTS})
        now = BASE + timedelta(hours=10)
        assert provider.latest("ETH", 10, now) == []
        assert provider.latest("btc", 10, now) == POINTS[:3]  # case-insensitive

    async def test_unsorted_input_is_normalized(self) -> None:
        provider = StaticFundingProvider({"BTC": [POINTS[2], POINTS[0], POINTS[1]]})
        now = BASE + timedelta(hours=10)
        assert [r.ts for r in provider.latest("BTC", 10, now)] == [p.ts for p in POINTS[:3]]


class TestEmptyFundingProvider:
    async def test_always_empty(self) -> None:
        provider = EmptyFundingProvider()
        await provider.update("BTC", BASE)
        assert provider.latest("BTC", 10, BASE + timedelta(hours=100)) == []


class TestDbFundingProviderCacheSlicing:
    async def test_latest_slices_the_cached_tail(self) -> None:
        provider = DbFundingProvider(None, cap=300)  # type: ignore[arg-type]
        provider._cache["BTC"] = POINTS[:3]
        now = BASE + timedelta(hours=10)
        assert provider.latest("BTC", 10, now) == POINTS[:3]
        assert provider.latest("BTC", 2, now) == POINTS[1:3]
        assert provider.latest("BTC", 0, now) == []
        assert provider.latest("ETH", 10, now) == []
