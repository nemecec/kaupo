"""KrakenVenue against a fake exchange. Zero network calls."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kaupo.domain import Candle, Order, OrderStatus, OrderType, Pair, Side, Timeframe
from kaupo.venues.kraken_client import AsyncBridge, ExchangeError, MarketMeta
from kaupo.venues.kraken_live import KrakenVenue, unattributed_order_id
from tests.fake_kraken import AlertSpy, FakeKrakenClient, unavailable

PAIR = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int, close: float = 100.0) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H4,
        ts=BASE + timedelta(hours=4 * i),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=10.0,
    )


@pytest.fixture
def bridge() -> Iterator[AsyncBridge]:
    b = AsyncBridge(name="test-bridge")
    yield b
    b.close()


@pytest.fixture
def client() -> FakeKrakenClient:
    return FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})


@pytest.fixture
def alerts() -> AlertSpy:
    return AlertSpy()


@pytest.fixture
def venue(bridge: AsyncBridge, client: FakeKrakenClient, alerts: AlertSpy) -> KrakenVenue:
    v = KrakenVenue(
        PAIR,
        client,
        bridge,
        max_notional=500.0,
        alert=alerts,
        retry_base_seconds=0.001,
    )
    v.on_candle(candle(0))  # establishes the reference price, like a real run
    return v


def market(side: Side = Side.BUY, size: float = 1.0) -> Order:
    return Order(pair=PAIR, side=side, order_type=OrderType.MARKET, size=size)


def limit(price: float, side: Side = Side.BUY, size: float = 1.0) -> Order:
    return Order(pair=PAIR, side=side, order_type=OrderType.LIMIT, size=size, limit_price=price)


class TestMarketOrders:
    def test_places_immediately_and_records_the_txid(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = market()
        venue.submit(order)
        assert len(client.placed) == 1
        assert client.placed[0].order_type == "market"
        assert client.placed[0].post_only is False
        assert order.exchange_order_id == client.placed[0].txid
        assert order.status is OrderStatus.OPEN

    def test_fill_carries_the_exchange_price_size_and_fee(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = market()
        venue.submit(order)
        client.fill_last(price=101.5, fee=0.26, ts=candle(1).ts)

        fills = venue.on_candle(candle(1))

        assert len(fills) == 1
        fill = fills[0]
        assert fill.order_id == order.id
        assert fill.price == 101.5  # the exchange's price, not the candle's
        assert fill.size == 1.0
        assert fill.fee == 0.26
        assert order.status is OrderStatus.FILLED
        assert order.filled_price == 101.5

    def test_several_trades_of_one_order_become_one_fill(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        venue.submit(market(size=2.0))
        txid = client.placed[-1].txid
        client.add_trade(txid, price=100.0, size=1.0, fee=0.1, ts=candle(1).ts)
        client.add_trade(txid, price=102.0, size=1.0, fee=0.1, ts=candle(1).ts)

        fills = venue.on_candle(candle(1))

        assert len(fills) == 1
        assert fills[0].size == 2.0
        assert fills[0].price == 101.0  # size-weighted average
        assert fills[0].fee == pytest.approx(0.2)


class TestPostOnlyLimits:
    def test_every_limit_carries_the_post_only_flag(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        venue.submit(limit(95.0))
        venue.submit(limit(105.0, side=Side.SELL))
        assert [p.post_only for p in client.placed] == [True, True]
        assert [p.order_type for p in client.placed] == ["limit", "limit"]
        assert [p.price for p in client.placed] == [95.0, 105.0]

    def test_marketable_rejection_expires_the_order_instead_of_erroring(
        self, bridge: AsyncBridge, alerts: AlertSpy
    ) -> None:
        client = FakeKrakenClient(reject_post_only=True)
        venue = KrakenVenue(PAIR, client, bridge, max_notional=500.0, alert=alerts)
        venue.on_candle(candle(0))
        order = limit(95.0)

        venue.submit(order)  # must not raise

        assert order.status is OrderStatus.CANCELLED
        assert venue.drain_expired() == [order]
        assert venue.on_candle(candle(1)) == []

    def test_unfilled_limit_is_cancelled_on_the_exchange_at_candle_close(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = limit(95.0)
        venue.submit(order)
        txid = client.placed[-1].txid

        fills = venue.on_candle(candle(1))

        assert fills == []
        assert client.cancelled == [txid]
        assert order.status is OrderStatus.CANCELLED
        assert venue.drain_expired() == [order]

    def test_cancel_precedes_the_trade_poll(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        # after a cancel returns no further fill can land, so the poll that
        # follows sees the final truth; the reverse order leaves a race
        venue.submit(limit(95.0))
        client.calls.clear()
        venue.on_candle(candle(1))
        assert client.calls.index("cancel_order") < client.calls.index("fetch_my_trades")


class TestPartialFills:
    def test_partial_fill_emits_one_fill_of_the_filled_size(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = limit(95.0, size=1.0)
        venue.submit(order)
        client.fill_last(price=95.0, size=0.4, fee=0.06, ts=candle(1).ts)

        fills = venue.on_candle(candle(1))

        assert len(fills) == 1
        assert fills[0].size == 0.4
        assert fills[0].price == 95.0
        # the order row keeps the requested size, the fill row what executed
        assert order.size == 1.0
        assert order.status is OrderStatus.FILLED
        assert venue.drain_expired() == []

    def test_partially_filled_order_is_still_cancelled_on_the_exchange(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        venue.submit(limit(95.0, size=1.0))
        txid = client.placed[-1].txid
        client.fill_last(price=95.0, size=0.4, fee=0.06, ts=candle(1).ts)
        venue.on_candle(candle(1))
        assert client.cancelled == [txid]


class TestSizing:
    def test_size_rounds_down_to_the_lot_step(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        venue.submit(market(size=1.2345))
        assert client.placed[0].size == 1.23  # step 0.01, never 1.24

    def test_below_the_minimum_size_the_order_is_skipped_and_alerted(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        order = market(size=0.02)  # min_amount is 0.05

        venue.submit(order)

        assert client.placed == []
        assert order.status is OrderStatus.REJECTED
        assert venue.drain_expired() == [order]
        assert alerts.matching("below the exchange minimum")

    def test_below_the_minimum_notional_the_order_is_skipped(
        self, bridge: AsyncBridge, alerts: AlertSpy
    ) -> None:
        client = FakeKrakenClient(
            market=MarketMeta(
                amount_step=Decimal("0.0001"), price_step=Decimal("0.01"), min_amount=0.0, min_cost=20.0
            )
        )
        venue = KrakenVenue(PAIR, client, bridge, max_notional=500.0, alert=alerts)
        venue.on_candle(candle(0, close=100.0))
        order = market(size=0.1)  # 10 EUR, under the 20 EUR minimum

        venue.submit(order)

        assert client.placed == []
        assert order.status is OrderStatus.REJECTED
        assert alerts.matching("below the exchange minimum")

    def test_size_that_rounds_to_zero_is_skipped_never_rounded_up(
        self, bridge: AsyncBridge, alerts: AlertSpy
    ) -> None:
        client = FakeKrakenClient(
            market=MarketMeta(
                amount_step=Decimal("1"), price_step=Decimal("0.01"), min_amount=0.0, min_cost=0.0
            )
        )
        venue = KrakenVenue(PAIR, client, bridge, max_notional=500.0, alert=alerts)
        venue.on_candle(candle(0))

        venue.submit(market(size=0.4))

        assert client.placed == []
        assert alerts.matching("rounds down to zero")

    def test_notional_over_the_cap_is_clamped_not_dropped(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        venue.submit(market(size=10.0))  # 1000 EUR at close 100, cap is 500
        assert client.placed[0].size == 5.0
        assert alerts.matching("notional cap")

    def test_a_limit_is_sized_off_its_own_price(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        venue.submit(limit(50.0, size=20.0))  # 1000 EUR at the limit price
        assert client.placed[0].size == 10.0  # clamped to the 500 EUR cap


class TestErrorHandling:
    def test_placement_is_never_retried(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        # a retry after an ambiguous timeout can place a second real order
        client.fail_next("create_order", unavailable(), times=3)
        order = market()

        venue.submit(order)

        assert client.calls.count("create_order") == 1
        assert order.status is OrderStatus.REJECTED
        assert venue.drain_expired() == [order]
        assert alerts.matching("rejected")

    def test_reads_are_retried(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        venue.submit(market())
        client.fill_last(price=100.0, fee=0.1, ts=candle(1).ts)
        client.fail_next("fetch_my_trades", unavailable())
        client.calls.clear()

        fills = venue.on_candle(candle(1))

        assert client.calls.count("fetch_my_trades") == 2
        assert len(fills) == 1

    def test_an_order_that_cannot_be_cancelled_stays_tracked(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        order = limit(95.0)
        venue.submit(order)
        client.fail_next("cancel_order", unavailable(), times=3)

        venue.on_candle(candle(1))

        assert order.status is OrderStatus.OPEN  # it can still fill
        assert venue.drain_expired() == []
        assert alerts.matching("could not cancel")

        client.fill_last(price=95.0, fee=0.1, ts=candle(2).ts)
        fills = venue.on_candle(candle(2))
        assert len(fills) == 1  # the retry on the next candle picks it up


class TestUnattributedTrades:
    def test_a_trade_of_an_unknown_order_is_recorded_and_alerted(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        client.add_trade("MANUAL-1", price=99.0, size=0.5, fee=0.1, ts=candle(1).ts, side=Side.BUY)

        fills = venue.on_candle(candle(1))

        assert len(fills) == 1
        assert fills[0].order_id == unattributed_order_id("MANUAL-1")
        assert fills[0].size == 0.5
        adopted = venue.drain_new_orders()
        assert [o.exchange_order_id for o in adopted] == ["MANUAL-1"]
        assert alerts.matching("unattributed")


class TestCancelAll:
    def test_cancels_tracked_orders_and_sweeps_the_rest(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        tracked = limit(95.0)
        venue.submit(tracked)
        client.open_txids.append("STRAY-1")  # left behind by an earlier process

        cancelled = venue.cancel_all()

        assert cancelled == [tracked]
        assert tracked.status is OrderStatus.CANCELLED
        assert set(client.cancelled) == {client.placed[0].txid, "STRAY-1"}

    def test_survives_an_exchange_failure(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        venue.submit(limit(95.0))
        client.fail_next("cancel_order", unavailable(), times=3)
        client.fail_next("fetch_open_orders", unavailable(), times=3)

        cancelled = venue.cancel_all()

        assert len(cancelled) == 1  # the engine still records them
        assert alerts.messages


class TestVoidFill:
    def test_shouts_and_changes_nothing(
        self, venue: KrakenVenue, client: FakeKrakenClient, alerts: AlertSpy
    ) -> None:
        order = market()
        venue.submit(order)
        client.fill_last(price=100.0, fee=0.1, ts=candle(1).ts)
        fill = venue.on_candle(candle(1))[0]

        venue.void_fill(fill)

        assert order.status is OrderStatus.FILLED  # the trade really happened
        assert alerts.matching("ledger divergence")


class TestLiquidate:
    def test_places_a_market_order_and_returns_the_real_fill(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        class _Filling(FakeKrakenClient):
            async def create_order(self, pair, side, order_type, size, price=None, *, post_only=False):  # type: ignore[no-untyped-def]
                txid = await super().create_order(pair, side, order_type, size, price, post_only=post_only)
                self.add_trade(txid, price=98.0, size=size, fee=0.25, ts=BASE, side=side)
                return txid

        filling = _Filling(balances={"EUR": 0.0, "SOL": 2.0})
        v = KrakenVenue(PAIR, filling, venue._bridge, max_notional=500.0, retry_base_seconds=0.001)
        v.on_candle(candle(1))

        fill = v.liquidate(PAIR, 2.0, candle(2))

        assert filling.placed[-1].order_type == "market"
        assert filling.placed[-1].side is Side.SELL
        assert fill.price == 98.0
        assert fill.size == 2.0
        assert fill.fee == 0.25

    def test_raises_when_the_order_never_trades(self, bridge: AsyncBridge, client: FakeKrakenClient) -> None:
        v = KrakenVenue(PAIR, client, bridge, max_notional=500.0, retry_base_seconds=0.001)
        v.on_candle(candle(1))
        with pytest.raises(ExchangeError):
            v.liquidate(PAIR, 1.0, candle(2))


class TestTradeCursor:
    def test_a_trade_is_never_returned_twice(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        venue.submit(market())
        client.fill_last(price=100.0, fee=0.1, ts=candle(1).ts)

        assert len(venue.on_candle(candle(1))) == 1
        assert venue.on_candle(candle(2)) == []  # same trade, not replayed
