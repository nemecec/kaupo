"""Order-flow providers: bucketing and point-in-time slicing (no DB)."""

from datetime import UTC, datetime, timedelta

from kaupo.core.orderflow import (
    DbOrderFlowProvider,
    EmptyOrderFlowProvider,
    StaticOrderFlowProvider,
    bucket_tick_flow,
)
from kaupo.domain import BookSnapshot, TickFlow, TradeTick

BASE = datetime(2026, 1, 1, tzinfo=UTC)
H1 = 3600


def tick(minutes: float, side: str = "buy", size: float = 1.0, pair: str = "BTC/EUR") -> TradeTick:
    return TradeTick(
        exchange="kraken", pair=pair, ts=BASE + timedelta(minutes=minutes), price=100.0, size=size, side=side
    )


def snapshot(hours: float, pair: str = "BTC/EUR") -> BookSnapshot:
    return BookSnapshot(
        exchange="kraken",
        pair=pair,
        ts=BASE + timedelta(hours=hours),
        bid=99.0,
        ask=101.0,
        bid_size=1.0,
        ask_size=2.0,
    )


# ticks straddling the hourly grid: 60m lands exactly on a candle open
# (bucket boundary), 100m mid-candle, 300m hours later
TICKS = [
    tick(30, "buy", 1.0),
    tick(60, "buy", 1.0),
    tick(90, "sell", 2.0),
    tick(100, "buy", 3.0),
    tick(300, "sell", 5.0),
]
BOOK = [snapshot(1), snapshot(2.5), snapshot(4.25), snapshot(20)]


class TestBucketTickFlow:
    def test_exact_candle_alignment(self) -> None:
        # a tick exactly on a candle open starts that candle's bucket
        flows = bucket_tick_flow(TICKS, 10, BASE + timedelta(hours=2), H1)
        assert [f.ts for f in flows] == [BASE, BASE + timedelta(hours=1)]
        first, second = flows
        assert first == TickFlow(
            ts=BASE, buy_count=1, sell_count=0, buy_volume=1.0, sell_volume=0.0, max_trade_size=1.0
        )
        assert second == TickFlow(
            ts=BASE + timedelta(hours=1),
            buy_count=2,
            sell_count=1,
            buy_volume=4.0,
            sell_volume=2.0,
            max_trade_size=3.0,
        )

    def test_partial_candle_excluded(self) -> None:
        # at 1h30 only bucket 0 is complete; the 60m/90m ticks sit in the
        # still-open bucket 1 and must not leak
        flows = bucket_tick_flow(TICKS, 10, BASE + timedelta(hours=1, minutes=30), H1)
        assert [f.ts for f in flows] == [BASE]
        # a tick exactly at now is in the in-progress bucket: excluded here,
        # while ticks() (at-or-before) would serve it
        flows = bucket_tick_flow(TICKS, 10, BASE + timedelta(hours=1), H1)
        assert [f.ts for f in flows] == [BASE]

    def test_n_slices_newest_buckets_and_candles_without_trades_absent(self) -> None:
        flows = bucket_tick_flow(TICKS, 10, BASE + timedelta(hours=6), H1)
        assert [f.ts for f in flows] == [BASE, BASE + timedelta(hours=1), BASE + timedelta(hours=5)]
        assert flows[-1].sell_volume == 5.0
        assert flows[-1].max_trade_size == 5.0
        newest_two = bucket_tick_flow(TICKS, 2, BASE + timedelta(hours=6), H1)
        assert [f.ts for f in newest_two] == [BASE + timedelta(hours=1), BASE + timedelta(hours=5)]

    def test_empty_and_nonpositive_n(self) -> None:
        now = BASE + timedelta(hours=6)
        assert bucket_tick_flow([], 10, now, H1) == []
        assert bucket_tick_flow(TICKS, 0, now, H1) == []
        assert bucket_tick_flow(TICKS, -1, now, H1) == []


class TestEmptyOrderFlowProvider:
    async def test_always_empty(self) -> None:
        provider = EmptyOrderFlowProvider()
        now = BASE + timedelta(hours=100)
        await provider.update("BTC/EUR", now)
        assert provider.ticks("BTC/EUR", 10, now) == []
        assert provider.book("BTC/EUR", 10, now) == []
        assert provider.tick_flow("BTC/EUR", 10, now, H1) == []


class TestStaticOrderFlowProvider:
    async def test_ticks_point_in_time_and_ascending(self) -> None:
        provider = StaticOrderFlowProvider(ticks={"BTC/EUR": TICKS})
        assert await provider.update("BTC/EUR", BASE) is None  # no-op

        assert provider.ticks("BTC/EUR", 10, BASE + timedelta(minutes=29)) == []
        # boundary is inclusive: a tick exactly at now is visible
        assert provider.ticks("BTC/EUR", 10, BASE + timedelta(minutes=30)) == TICKS[:1]
        assert provider.ticks("BTC/EUR", 10, BASE + timedelta(minutes=90)) == TICKS[:3]
        assert provider.ticks("BTC/EUR", 10, BASE + timedelta(hours=100)) == TICKS

    async def test_book_point_in_time(self) -> None:
        provider = StaticOrderFlowProvider(book={"BTC/EUR": BOOK})
        now = BASE + timedelta(hours=3)
        assert provider.book("BTC/EUR", 10, now) == BOOK[:2]
        assert provider.book("BTC/EUR", 10, BASE + timedelta(hours=100)) == BOOK

    async def test_n_slices_the_newest(self) -> None:
        provider = StaticOrderFlowProvider(ticks={"BTC/EUR": TICKS}, book={"BTC/EUR": BOOK})
        now = BASE + timedelta(hours=100)
        assert provider.ticks("BTC/EUR", 2, now) == TICKS[-2:]
        assert provider.book("BTC/EUR", 1, now) == BOOK[-1:]
        assert provider.ticks("BTC/EUR", 0, now) == []
        assert provider.book("BTC/EUR", -1, now) == []
        assert provider.tick_flow("BTC/EUR", 0, now, H1) == []

    async def test_unknown_pair_and_unsorted_input(self) -> None:
        provider = StaticOrderFlowProvider(ticks={"BTC/EUR": [TICKS[3], TICKS[0], TICKS[1]]})
        now = BASE + timedelta(hours=100)
        assert provider.ticks("ETH/EUR", 10, now) == []
        assert provider.book("BTC/EUR", 10, now) == []  # no book series given
        got = provider.ticks("BTC/EUR", 10, now)
        assert [t.ts for t in got] == [t.ts for t in (TICKS[0], TICKS[1], TICKS[3])]

    async def test_tick_flow_point_in_time_completed_only(self) -> None:
        provider = StaticOrderFlowProvider(ticks={"BTC/EUR": TICKS})
        # mid-candle: only the first (completed) bucket
        flows = provider.tick_flow("BTC/EUR", 10, BASE + timedelta(hours=1, minutes=30), H1)
        assert [f.ts for f in flows] == [BASE]
        # later clock: completed buckets accumulate, ascending
        flows = provider.tick_flow("BTC/EUR", 10, BASE + timedelta(hours=6), H1)
        assert [f.ts for f in flows] == [BASE, BASE + timedelta(hours=1), BASE + timedelta(hours=5)]
        assert provider.tick_flow("ETH/EUR", 10, BASE + timedelta(hours=6), H1) == []


class TestDbOrderFlowProviderCacheSlicing:
    async def test_accessors_slice_the_cached_tail(self) -> None:
        provider = DbOrderFlowProvider(None)  # type: ignore[arg-type]
        provider._ticks["BTC/EUR"] = TICKS[:4]
        provider._book["BTC/EUR"] = BOOK[:3]
        now = BASE + timedelta(hours=2)

        assert provider.ticks("BTC/EUR", 10, now) == TICKS[:4]
        assert provider.ticks("BTC/EUR", 2, now) == TICKS[2:4]
        assert provider.ticks("BTC/EUR", 0, now) == []
        assert provider.book("BTC/EUR", 10, now) == BOOK[:3]
        assert provider.book("BTC/EUR", 1, now) == BOOK[2:3]
        assert provider.book("BTC/EUR", -1, now) == []
        assert provider.ticks("ETH/EUR", 10, now) == []
        assert provider.book("ETH/EUR", 10, now) == []

        flows = provider.tick_flow("BTC/EUR", 10, now, H1)
        assert [f.ts for f in flows] == [BASE, BASE + timedelta(hours=1)]
        assert flows[1].buy_volume == 4.0
        assert provider.tick_flow("ETH/EUR", 10, now, H1) == []
