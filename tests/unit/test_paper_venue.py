from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kaupo.domain import Candle, Order, OrderStatus, OrderType, Pair, Side, Timeframe
from kaupo.venues.paper import PaperVenue

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int, o: float = 100, h: float = 101, low: float = 99, c: float = 100) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
    )


def venue() -> PaperVenue:
    return PaperVenue(taker_fee_bps=100, maker_fee_bps=50, slippage_bps=100)  # 1% each for easy math


def market(side: Side = Side.BUY, size: float = 1.0, **kw: object) -> Order:
    return Order(pair=PAIR, side=side, order_type=OrderType.MARKET, size=size, **kw)  # type: ignore[arg-type]


class TestMarketOrders:
    def test_fills_at_next_open_with_slippage_and_taker_fee(self) -> None:
        v = venue()
        order = market()
        v.submit(order)
        # eligible on the next candle processed after submission
        fills = v.on_candle(candle(1, o=200))
        assert len(fills) == 1
        assert fills[0].price == 200 * 1.01  # buy slipped up
        assert fills[0].fee == 202 * 0.01
        assert order.status is OrderStatus.FILLED

    def test_sell_slipped_down(self) -> None:
        v = venue()
        v.submit(market(Side.SELL))
        fills = v.on_candle(candle(0, o=100))
        assert fills[0].price == 99.0


class TestLimitOrders:
    def test_buy_fills_when_low_touches(self) -> None:
        v = venue()
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=95.0)
        v.submit(order)
        fills = v.on_candle(candle(0, low=94))
        assert len(fills) == 1
        assert fills[0].price == 95.0
        assert fills[0].fee == 95.0 * 0.005  # maker
        assert order.status is OrderStatus.FILLED

    def test_gap_through_gets_open(self) -> None:
        v = venue()
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=95.0)
        v.submit(order)
        fills = v.on_candle(candle(0, o=90, low=89))
        assert fills[0].price == 90.0

    def test_sell_fills_when_high_touches(self) -> None:
        v = venue()
        order = Order(pair=PAIR, side=Side.SELL, order_type=OrderType.LIMIT, size=1.0, limit_price=105.0)
        v.submit(order)
        fills = v.on_candle(candle(0, h=106))
        assert len(fills) == 1
        assert fills[0].price == 105.0
        assert fills[0].fee == 105.0 * 0.005  # maker

    def test_fill_at_limit_has_no_slippage(self) -> None:
        """Open exactly at the limit: fills at the limit, not the slipped price."""
        v = venue()
        order = Order(pair=PAIR, side=Side.SELL, order_type=OrderType.LIMIT, size=1.0, limit_price=105.0)
        v.submit(order)
        fills = v.on_candle(candle(0, o=105, h=106))
        assert fills[0].price == 105.0  # a market sell would fill at 105 * 0.99

    def test_untouched_expires_at_candle_close(self) -> None:
        """One-candle lifetime: no rest, no second chance, expiry is drained once."""
        v = venue()
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=95.0)
        v.submit(order)
        assert v.on_candle(candle(0, low=96)) == []  # never touched
        assert order.status is OrderStatus.CANCELLED
        assert v.drain_expired() == [order]
        assert v.drain_expired() == []  # drained once
        # a later candle through the limit does NOT fill the expired order
        assert v.on_candle(candle(1, low=50)) == []

    def test_filled_limit_is_not_reported_expired(self) -> None:
        v = venue()
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=95.0)
        v.submit(order)
        v.on_candle(candle(0, low=94))
        assert v.drain_expired() == []

    def test_cancel_all_cancels_unprocessed_limit(self) -> None:
        """A limit submitted after the last candle is cancelled at wind-down."""
        v = venue()
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=95.0)
        v.submit(order)
        cancelled = v.cancel_all()
        assert cancelled == [order]
        assert order.status is OrderStatus.CANCELLED


class TestProtection:
    def test_stop_loss_triggers_and_slips(self) -> None:
        v = venue()
        order = market(stop_loss=90.0)
        v.submit(order)
        v.on_candle(candle(0, o=100))  # fills at 101, protection armed
        fills = v.on_candle(candle(1, low=89))
        assert len(fills) == 1
        assert fills[0].side is Side.SELL
        assert fills[0].price == 90 * 0.99  # slipped down, taker
        assert "protection" in v.get_order(fills[0].order_id).reason  # type: ignore[union-attr]

    def test_take_profit_triggers(self) -> None:
        v = venue()
        order = market(take_profit=110.0)
        v.submit(order)
        v.on_candle(candle(0, o=100))
        fills = v.on_candle(candle(1, h=111))
        assert len(fills) == 1
        assert fills[0].price == 110.0
        assert fills[0].fee == 110.0 * 0.005  # maker

    def test_stop_wins_when_both_hit(self) -> None:
        v = venue()
        order = market(stop_loss=90.0, take_profit=110.0)
        v.submit(order)
        v.on_candle(candle(0, o=100))
        fills = v.on_candle(candle(1, low=85, h=115))
        assert len(fills) == 1
        assert fills[0].price == 90 * 0.99  # stop executed, not TP

    def test_protection_arms_after_limit_fill(self) -> None:
        v = venue()
        order = Order(
            pair=PAIR, side=Side.BUY, order_type=OrderType.LIMIT, size=1.0, limit_price=50.0, stop_loss=45.0
        )
        v.submit(order)
        # low 44 <= limit 50 -> fills at 50, protection armed for following candles
        fills = v.on_candle(candle(0, low=44))
        assert len(fills) == 1
        assert fills[0].price == 50.0
        fills = v.on_candle(candle(1, low=44))
        assert len(fills) == 1
        assert fills[0].price == 45 * 0.99  # stop slipped down


class TestLiquidate:
    def test_liquidate_at_slipped_close(self) -> None:
        v = venue()
        fill = v.liquidate(PAIR, 2.0, candle(0, c=100))
        assert fill.price == 99.0
        assert fill.size == 2.0
        assert v.get_order(fill.order_id) is not None


class TestProtectionPositionAwareness:
    def test_protection_disarmed_after_strategy_exit(self) -> None:
        """buy with stop -> strategy exit -> candle through old stop is a no-op."""
        v = venue()
        buy = market(stop_loss=90.0)
        v.submit(buy)
        v.on_candle(candle(0, o=100))  # buy fills at 101, protection armed
        # strategy exits manually
        v.submit(market(Side.SELL))
        fills = v.on_candle(candle(1, o=100))
        assert len(fills) == 1 and fills[0].side is Side.SELL  # position now 0
        # price later crashes through the old stop: protection must NOT fire
        assert v.on_candle(candle(2, low=50)) == []

    def test_protection_clamped_to_remaining_position(self) -> None:
        v = venue()
        v.submit(market(size=2.0, stop_loss=90.0))
        v.on_candle(candle(0, o=100))  # buy 2 @ 101
        v.submit(market(Side.SELL, size=1.0))
        v.on_candle(candle(1, o=100))  # strategy sells 1 -> 1 left
        fills = v.on_candle(candle(2, low=89))
        assert len(fills) == 1
        assert fills[0].size == 1.0  # clamped from original 2 to remaining 1

    def test_partial_stop_keeps_watching_remainder(self) -> None:
        v = venue()
        v.submit(market(size=2.0, stop_loss=90.0))
        v.on_candle(candle(0, o=100))
        v.submit(market(Side.SELL, size=1.0))
        v.on_candle(candle(1, o=100))  # 1 left
        fills = v.on_candle(candle(2, low=89))
        assert fills[0].size == 1.0
        # position now 0; nothing left to protect
        assert v.on_candle(candle(3, low=10)) == []

    def test_cancel_all_clears_protections(self) -> None:
        v = venue()
        v.submit(market(stop_loss=90.0))
        v.on_candle(candle(0, o=100))
        v.cancel_all()
        assert v.on_candle(candle(1, low=50)) == []

    def test_venue_created_orders_use_candle_time(self) -> None:
        v = venue()
        fill = v.liquidate(PAIR, 1.0, candle(7, c=100))
        order = v.get_order(fill.order_id)
        assert order is not None
        assert order.created_ts == candle(7).ts

        v2 = venue()
        v2.submit(market(stop_loss=90.0))
        v2.on_candle(candle(0, o=100))
        fills = v2.on_candle(candle(1, low=89))
        prot_order = v2.get_order(fills[0].order_id)
        assert prot_order is not None
        assert prot_order.created_ts == candle(1).ts

    def test_filled_orders_pruned(self) -> None:
        v = venue()
        order = market()
        v.submit(order)
        v.on_candle(candle(0, o=100))  # fills
        assert v.get_order(order.id) is not None
        v.on_candle(candle(1))  # next candle prunes closed orders
        assert v.get_order(order.id) is None


class TestChronologicalOrdering:
    def test_open_fill_lands_before_intracandle_protection(self) -> None:
        """Strategy exit at the open wins over a stop that triggers later in
        the candle; the protection is disarmed and no double-sell happens."""
        v = venue()
        v.submit(market(stop_loss=95.0))
        v.on_candle(candle(0, o=100))  # buy fills at 101
        # strategy exit queued; next candle opens 100, dips through the stop
        v.submit(market(Side.SELL))
        fills = v.on_candle(candle(1, o=100, low=94))
        assert len(fills) == 1  # only the open exit; protection saw flat position
        assert fills[0].price == 99.0  # open slipped 1%, NOT the stop's 95-ish
        assert fills[0].side is Side.SELL

    def test_multiple_protections_track_incrementally(self) -> None:
        """Two armed positions; both stops hit in one candle — second clamps
        to what the first left over."""
        v = venue()
        v.submit(market(size=1.0, stop_loss=95.0))
        v.on_candle(candle(0, o=100))
        v.submit(market(size=1.0, stop_loss=95.0))
        v.on_candle(candle(1, o=100))  # two longs of 1.0, both protected
        fills = v.on_candle(candle(2, low=90))  # both stops trigger
        assert len(fills) == 2
        assert sum(f.size for f in fills) == 2.0  # exactly the position, no oversell
        assert v._positions[PAIR] == 0.0

    def test_void_fill_rolls_back(self) -> None:
        v = venue()
        order = market()
        v.submit(order)
        fills = v.on_candle(candle(0, o=100))
        assert v._positions[PAIR] == 1.0
        v.void_fill(fills[0])
        assert v._positions[PAIR] == 0.0
        assert order.status is OrderStatus.REJECTED
        assert order.filled_price is None


class TestVoidProtectionLifecycle:
    def test_voided_entry_drops_its_protection(self) -> None:
        v = venue()
        v.submit(market(stop_loss=90.0))
        fills = v.on_candle(candle(0, o=100))
        assert len(v._protections) == 1
        v.void_fill(fills[0])
        assert v._protections == []
        # nothing fires later
        assert v.on_candle(candle(1, low=50)) == []

    def test_voided_protection_exit_re_arms(self) -> None:
        v = venue()
        v.submit(market(stop_loss=90.0))
        v.on_candle(candle(0, o=100))  # buy fills, protection armed
        fills = v.on_candle(candle(1, low=85))  # stop fires
        assert len(fills) == 1
        assert v._protections == []  # consumed
        v.void_fill(fills[0])  # ledger rejected the exit
        assert len(v._protections) == 1  # re-armed
        # and it can fire again
        fills = v.on_candle(candle(2, low=85))
        assert len(fills) == 1
        assert fills[0].side is Side.SELL

    def test_fractional_sizes_no_drift(self) -> None:
        v = venue()
        for _ in range(3):
            v.submit(market(size=0.1))
            v.on_candle(candle(0, o=100))
        assert v._positions[PAIR] == Decimal("0.3")  # not 0.30000000000000004
        v.submit(market(Side.SELL, size=0.3))
        v.on_candle(candle(1, o=100))
        assert v._positions[PAIR] == Decimal("0")

    def test_same_candle_round_trip_disarms_old_protection(self) -> None:
        v = venue()
        v.submit(market(stop_loss=90.0))
        v.on_candle(candle(0, o=100))  # buy with stop 90
        # strategy exits AND re-enters in one batch (both fill at next open)
        v.submit(market(Side.SELL))
        v.submit(market(size=1.0))  # re-entry WITHOUT stop
        v.on_candle(candle(1, o=110))
        # old stop must not fire against the new position
        assert v.on_candle(candle(2, low=85)) == []


def test_protections_clamp_incrementally_against_partial_exit() -> None:
    """Two protected entries + same-candle partial strategy exit: protections
    must sell only what remains after each prior fill."""
    v = venue()
    v.submit(market(size=2.0, stop_loss=90.0))
    v.on_candle(candle(0, o=100))
    v.submit(market(size=2.0, stop_loss=90.0))
    v.on_candle(candle(1, o=100))  # 4.0 total, two protections
    v.submit(market(Side.SELL, size=3.0))  # strategy scale-out
    fills = v.on_candle(candle(2, o=100, low=85))  # open exit, then both stops
    sells = [f for f in fills if f.side is Side.SELL]
    assert sum(f.size for f in sells) == 4.0  # never more than the position
    assert v._positions[PAIR] == 0
