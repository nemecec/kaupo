"""Fee-tier fidelity on limits marketable at posting (kaupo#36).

A limit that would take liquidity the moment it rests on the book is not a
maker order on any real venue. The three modes are the three plausible
venues: charge maker anyway (legacy), charge taker (a plain limit), or never
fill (Kraken post-only, which is what the live venue posts).
"""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.domain import Candle, Order, OrderStatus, OrderType, Pair, Side, Timeframe
from kaupo.venues.paper import (
    DEFAULT_MARKETABLE_LIMIT,
    LIVE_MIRROR_MARKETABLE_LIMIT,
    MARKETABLE_LIMIT_MODES,
    MarketableLimit,
    PaperVenue,
    is_marketable_at_posting,
)

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)

# 1% taker, 0.5% maker, 1% slippage — the same easy math as test_paper_venue
TAKER_BPS = 100.0
MAKER_BPS = 50.0
TAKER_RATE = TAKER_BPS / 10_000
MAKER_RATE = MAKER_BPS / 10_000


def candle(open_: float = 100.0, high: float = 106.0, low: float = 94.0, close: float = 100.0) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=1),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )


def venue(mode: MarketableLimit) -> PaperVenue:
    return PaperVenue(TAKER_BPS, MAKER_BPS, slippage_bps=100, marketable_limit=mode)


def limit(side: Side, price: float, size: float = 1.0) -> Order:
    return Order(pair=PAIR, side=side, order_type=OrderType.LIMIT, size=size, limit_price=price)


class TestTheRule:
    """buy: limit >= open; sell: limit <= open. Boundaries included."""

    @pytest.mark.parametrize(
        ("side", "limit_price", "expected"),
        [
            (Side.BUY, 105.0, True),  # crosses the ask
            (Side.BUY, 100.0, True),  # exactly at the open: still marketable
            (Side.BUY, 95.0, False),  # rests below the market
            (Side.SELL, 95.0, True),  # crosses the bid
            (Side.SELL, 100.0, True),  # exactly at the open
            (Side.SELL, 105.0, False),  # rests above the market
        ],
    )
    def test_marketability_at_the_open(self, side: Side, limit_price: float, expected: bool) -> None:
        assert is_marketable_at_posting(side, limit_price, 100.0) is expected

    def test_a_marketable_limit_is_always_touched(self) -> None:
        """So the modes only ever change an order that would have filled."""
        c = candle(open_=100.0, high=100.0, low=100.0)  # a flat candle, no range
        for side, price in ((Side.BUY, 100.0), (Side.SELL, 100.0)):
            assert is_marketable_at_posting(side, price, c.open)
            v = venue("maker")
            v.submit(limit(side, price))
            assert len(v.on_candle(c)) == 1


class TestMakerMode:
    """The legacy model: fill at limit or gap-open, maker fee, always."""

    def test_marketable_buy_fills_at_the_open_charged_maker(self) -> None:
        v = venue("maker")
        order = limit(Side.BUY, 105.0)
        v.submit(order)

        fills = v.on_candle(candle(open_=100.0))

        assert len(fills) == 1
        assert fills[0].price == 100.0  # gapped through -> the open
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * MAKER_RATE)
        assert order.status is OrderStatus.FILLED
        assert v.drain_expired() == []

    def test_marketable_sell_fills_at_the_open_charged_maker(self) -> None:
        v = venue("maker")
        v.submit(limit(Side.SELL, 95.0))

        fills = v.on_candle(candle(open_=100.0))

        assert fills[0].price == 100.0
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * MAKER_RATE)

    def test_it_is_the_default(self) -> None:
        assert DEFAULT_MARKETABLE_LIMIT == "maker"
        legacy = PaperVenue(TAKER_BPS, MAKER_BPS, 100)
        legacy.submit(limit(Side.SELL, 95.0))
        fills = legacy.on_candle(candle(open_=100.0))
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * MAKER_RATE)


class TestTakerMode:
    """A plain limit on a real venue: the limit bounds the price, taker fee."""

    def test_marketable_buy_pays_taker_at_the_bounded_price(self) -> None:
        v = venue("taker")
        v.submit(limit(Side.BUY, 105.0))

        fills = v.on_candle(candle(open_=100.0))

        assert len(fills) == 1
        assert fills[0].price == 100.0  # min(limit, open); no slippage, the limit bounds it
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * TAKER_RATE)

    def test_marketable_sell_pays_taker_at_the_bounded_price(self) -> None:
        v = venue("taker")
        v.submit(limit(Side.SELL, 95.0))

        fills = v.on_candle(candle(open_=100.0))

        assert fills[0].price == 100.0  # max(limit, open)
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * TAKER_RATE)

    def test_at_the_open_exactly_still_pays_taker(self) -> None:
        v = venue("taker")
        v.submit(limit(Side.SELL, 100.0))

        fills = v.on_candle(candle(open_=100.0))

        assert fills[0].price == 100.0
        assert fills[0].fee == pytest.approx(100.0 * 1.0 * TAKER_RATE)

    def test_the_fee_is_the_only_difference_from_maker_mode(self) -> None:
        maker_v, taker_v = venue("maker"), venue("taker")
        maker_v.submit(limit(Side.SELL, 95.0))
        taker_v.submit(limit(Side.SELL, 95.0))
        c = candle(open_=100.0)

        maker_fill = maker_v.on_candle(c)[0]
        taker_fill = taker_v.on_candle(c)[0]

        assert taker_fill.price == maker_fill.price
        assert taker_fill.size == maker_fill.size
        assert taker_fill.fee == pytest.approx(maker_fill.fee * (TAKER_RATE / MAKER_RATE))


class TestSkipMode:
    """Kraken post-only: a marketable limit is rejected and never fills."""

    def test_marketable_buy_does_not_fill_and_expires(self) -> None:
        v = venue("skip")
        order = limit(Side.BUY, 105.0)
        v.submit(order)

        fills = v.on_candle(candle(open_=100.0))

        assert fills == []
        assert order.status is OrderStatus.CANCELLED
        assert v.drain_expired() == [order]

    def test_marketable_sell_does_not_fill_and_expires(self) -> None:
        v = venue("skip")
        order = limit(Side.SELL, 95.0)
        v.submit(order)

        assert v.on_candle(candle(open_=100.0)) == []
        assert v.drain_expired() == [order]

    def test_at_the_open_exactly_is_skipped(self) -> None:
        v = venue("skip")
        order = limit(Side.SELL, 100.0)
        v.submit(order)

        assert v.on_candle(candle(open_=100.0)) == []
        assert v.drain_expired() == [order]

    def test_a_skipped_order_leaves_no_position_behind(self) -> None:
        v = venue("skip")
        v.submit(limit(Side.BUY, 105.0))
        v.on_candle(candle(open_=100.0))
        v.drain_expired()
        # the order is gone, not resting: a later candle produces nothing
        assert v.on_candle(candle(open_=100.0)) == []
        assert v.drain_expired() == []

    def test_it_is_the_mode_shadow_and_live_share(self) -> None:
        assert LIVE_MIRROR_MARKETABLE_LIMIT == "skip"


class TestNonMarketableLimitsAreUntouched:
    """Every mode keeps the touch rule and the maker fee for a resting limit."""

    @pytest.mark.parametrize("mode", MARKETABLE_LIMIT_MODES)
    def test_buy_below_the_open_fills_at_the_limit_charged_maker(self, mode: MarketableLimit) -> None:
        v = venue(mode)
        order = limit(Side.BUY, 95.0)
        v.submit(order)

        fills = v.on_candle(candle(open_=100.0, low=94.0))

        assert len(fills) == 1
        assert fills[0].price == 95.0  # the limit, not the open
        assert fills[0].fee == pytest.approx(95.0 * 1.0 * MAKER_RATE)
        assert order.status is OrderStatus.FILLED

    @pytest.mark.parametrize("mode", MARKETABLE_LIMIT_MODES)
    def test_sell_above_the_open_fills_at_the_limit_charged_maker(self, mode: MarketableLimit) -> None:
        v = venue(mode)
        v.submit(limit(Side.SELL, 105.0))

        fills = v.on_candle(candle(open_=100.0, high=106.0))

        assert fills[0].price == 105.0
        assert fills[0].fee == pytest.approx(105.0 * 1.0 * MAKER_RATE)

    @pytest.mark.parametrize("mode", MARKETABLE_LIMIT_MODES)
    def test_an_untouched_limit_expires_in_every_mode(self, mode: MarketableLimit) -> None:
        v = venue(mode)
        order = limit(Side.BUY, 90.0)
        v.submit(order)

        assert v.on_candle(candle(open_=100.0, low=95.0)) == []
        assert v.drain_expired() == [order]


class TestGapThrough:
    """A gap-through limit is a marketable limit, so it follows the mode."""

    @pytest.mark.parametrize(
        ("mode", "expected_rate"),
        [("maker", MAKER_RATE), ("taker", TAKER_RATE)],
    )
    def test_a_gapped_sell_fills_at_the_open_at_the_mode_rate(
        self, mode: MarketableLimit, expected_rate: float
    ) -> None:
        # this is ticket kaupo#36's second piece of evidence in miniature:
        # the candle opened above the limit, so the book was already through it
        v = venue(mode)
        v.submit(limit(Side.SELL, 99.0))

        fills = v.on_candle(candle(open_=101.0, high=102.0, low=100.0))

        assert fills[0].price == 101.0  # max(limit, open)
        assert fills[0].fee == pytest.approx(101.0 * 1.0 * expected_rate)

    def test_a_gapped_sell_is_skipped_in_skip_mode(self) -> None:
        v = venue("skip")
        order = limit(Side.SELL, 99.0)
        v.submit(order)

        assert v.on_candle(candle(open_=101.0, high=102.0, low=100.0)) == []
        assert v.drain_expired() == [order]


class TestConstruction:
    def test_the_three_positional_fee_arguments_still_work(self) -> None:
        v = PaperVenue(26.0, 16.0, 5.0)
        v.submit(limit(Side.SELL, 105.0))
        assert v.on_candle(candle(open_=100.0, high=106.0))[0].fee == pytest.approx(105.0 * 0.0016)

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="marketable_limit"):
            PaperVenue(26.0, 16.0, 5.0, marketable_limit="post-only")  # type: ignore[arg-type]

    def test_the_mode_list_matches_the_type(self) -> None:
        assert MARKETABLE_LIMIT_MODES == ("maker", "taker", "skip")
