"""Fee-tier and tick fidelity on the live path (kaupo#42, kaupo#44).

The first live fill raised three questions the platform could not answer
from its own records: what price did it actually send, which fee tier did
the fill pay, and in what currency was the fee charged. Answering the first
one needed a reconstruction of ccxt's rounding, and the other two needed a
manual Kraken lookup. All three are recorded now.
"""

from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.backtest.run import BacktestRequest
from kaupo.config import default_maker_bps, default_taker_bps, get_settings
from kaupo.core.live_runner import LiveRequest
from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest
from kaupo.domain import Candle, Fill, Order, OrderId, OrderType, Pair, Side, Timeframe
from kaupo.risk.manager import RiskConfig
from kaupo.venues.kraken_client import (
    AsyncBridge,
    ExchangeTrade,
    MarketMeta,
    round_price_to_step,
)
from kaupo.venues.kraken_live import KrakenVenue, aggregate_trades
from kaupo.venues.paper import PaperVenue
from tests.fake_kraken import AlertSpy, FakeKrakenClient

PAIR = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
TICK = Decimal("0.01")  # the real SOL/EUR tick

# the real order from kaupo#42: the strategy asked for this price
LIVE_LIMIT = 87.38705


def candle(i: int, close: float = 90.0, low: float | None = None) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=Timeframe.H4,
        ts=BASE + timedelta(hours=4 * i),
        open=close,
        high=close * 1.01,
        low=close * 0.99 if low is None else low,
        close=close,
        volume=10.0,
    )


@pytest.fixture
def bridge() -> Iterator[AsyncBridge]:
    b = AsyncBridge(name="test-tick-bridge")
    yield b
    b.close()


@pytest.fixture
def client() -> FakeKrakenClient:
    return FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})


@pytest.fixture
def venue(bridge: AsyncBridge, client: FakeKrakenClient) -> KrakenVenue:
    v = KrakenVenue(PAIR, client, bridge, max_notional=500.0, alert=AlertSpy())
    v.on_candle(candle(0))  # establishes the reference price, like a real run
    return v


def limit(price: float, side: Side = Side.BUY, size: float = 0.29) -> Order:
    return Order(pair=PAIR, side=side, order_type=OrderType.LIMIT, size=size, limit_price=price)


def trade(fee: float = 0.1, taker_or_maker: str | None = None, trade_id: str = "T1") -> ExchangeTrade:
    return ExchangeTrade(
        id=trade_id,
        txid="OXVFKA",
        ts=BASE,
        side=Side.BUY,
        price=87.39,
        size=0.28522532,
        fee=fee,
        fee_currency="EUR",
        taker_or_maker=taker_or_maker,
    )


class TestRounding:
    """A buy rounds down and a sell rounds up: never keener than asked."""

    @pytest.mark.parametrize(
        ("side", "price", "expected"),
        [
            (Side.BUY, LIVE_LIMIT, 87.38),  # ccxt sent 87.39 instead
            (Side.SELL, LIVE_LIMIT, 87.39),
            (Side.BUY, 87.39, 87.39),  # already on the tick: untouched
            (Side.SELL, 87.39, 87.39),
            (Side.BUY, 87.381, 87.38),
            (Side.SELL, 87.381, 87.39),
        ],
    )
    def test_the_direction_is_conservative(self, side: Side, price: float, expected: float) -> None:
        assert round_price_to_step(price, TICK, side) == pytest.approx(expected)

    @pytest.mark.parametrize("price", [87.38705, 87.381, 87.389, 0.000123, 1234.5678])
    def test_a_buy_never_moves_up_and_a_sell_never_moves_down(self, price: float) -> None:
        assert round_price_to_step(price, TICK, Side.BUY) <= price
        assert round_price_to_step(price, TICK, Side.SELL) >= price

    def test_a_venue_without_a_tick_leaves_the_price_alone(self) -> None:
        assert round_price_to_step(LIVE_LIMIT, Decimal(0), Side.BUY) == LIVE_LIMIT


class TestTheVenueSendsAndRecordsTheSamePrice:
    """kaupo#42 anomaly 2: the record disagreed with the exchange."""

    def test_a_buy_limit_is_rounded_down_before_placement(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = limit(LIVE_LIMIT)

        venue.submit(order)

        assert client.placed[-1].price == pytest.approx(87.38)
        assert order.limit_price == pytest.approx(87.38)  # the record matches

    def test_a_sell_limit_is_rounded_up_before_placement(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = limit(LIVE_LIMIT, side=Side.SELL)

        venue.submit(order)

        assert client.placed[-1].price == pytest.approx(87.39)
        assert order.limit_price == pytest.approx(87.39)

    def test_a_buy_can_no_longer_fill_above_its_own_recorded_limit(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        # the exact shape of kaupo#42: filled_price 87.39 over limit_price
        # 87.38705, which the platform's own model calls impossible
        order = limit(LIVE_LIMIT)
        venue.submit(order)
        sent = client.placed[-1].price
        assert sent is not None
        client.fill_last(price=sent, fee=0.0997, ts=candle(1).ts, taker_or_maker="maker")

        venue.on_candle(candle(1))

        assert order.filled_price is not None
        assert order.limit_price is not None
        assert order.filled_price <= order.limit_price

    def test_a_market_order_keeps_its_absent_price(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = Order(pair=PAIR, side=Side.BUY, order_type=OrderType.MARKET, size=0.29)

        venue.submit(order)

        assert client.placed[-1].price is None
        assert order.limit_price is None


class TestTheFeeTierIsRecorded:
    """kaupo#42 anomaly 1: only Kraken knew whether the fee was the maker fee."""

    def test_the_exchange_verdict_reaches_the_fill_and_the_order(
        self, venue: KrakenVenue, client: FakeKrakenClient
    ) -> None:
        order = limit(87.38)
        venue.submit(order)
        client.fill_last(price=87.38, fee=0.0997, ts=candle(1).ts, taker_or_maker="maker")

        fills = venue.on_candle(candle(1))

        assert fills[0].taker_or_maker == "maker"
        assert fills[0].fee_currency == "EUR"
        assert order.taker_or_maker == "maker"
        assert order.fee_currency == "EUR"

    def test_a_taker_fill_is_recorded_as_taker(self, venue: KrakenVenue, client: FakeKrakenClient) -> None:
        order = limit(87.38)
        venue.submit(order)
        client.fill_last(price=87.38, fee=0.1994, ts=candle(1).ts, taker_or_maker="taker")

        assert venue.on_candle(candle(1))[0].taker_or_maker == "taker"

    def test_one_order_filling_on_both_sides_is_called_mixed(self) -> None:
        fill = aggregate_trades(
            OrderId("o1"),
            PAIR,
            [trade(taker_or_maker="maker"), trade(taker_or_maker="taker", trade_id="T2")],
        )
        assert fill.taker_or_maker == "mixed"

    def test_a_venue_that_reports_no_tier_records_none(self) -> None:
        fill = aggregate_trades(OrderId("o1"), PAIR, [trade()])
        assert fill.taker_or_maker is None
        assert fill.fee_currency == "EUR"


class TestThePaperVenueRecordsTheTierItModelled:
    """So a shadow twin and its live run state the same fact."""

    def _venue(self) -> PaperVenue:
        return PaperVenue(taker_fee_bps=80, maker_fee_bps=40, slippage_bps=5)

    def test_a_market_order_pays_taker(self) -> None:
        v = self._venue()
        v.submit(Order(pair=PAIR, side=Side.BUY, order_type=OrderType.MARKET, size=1.0))

        fills = v.on_candle(candle(1, close=100.0))

        assert fills[0].taker_or_maker == "taker"
        assert fills[0].fee_currency == "EUR"

    def test_a_resting_limit_pays_maker(self) -> None:
        v = self._venue()
        v.submit(limit(95.0, size=1.0))

        fills = v.on_candle(candle(1, close=100.0, low=94.0))

        assert fills[0].taker_or_maker == "maker"


class TestTheModelledTierMatchesTheAccount:
    """kaupo#44: 26/16 described a volume tier this account does not hold."""

    def test_the_defaults_are_the_account_tier(self) -> None:
        settings = get_settings()
        assert settings.default_taker_fee_bps == 80.0
        assert settings.default_maker_fee_bps == 40.0

    @pytest.mark.parametrize(
        "request_type",
        [BacktestRequest, PortfolioBacktestRequest, ShadowRequest, PortfolioShadowRequest, LiveRequest],
    )
    def test_every_request_reads_one_source(self, request_type: type) -> None:
        by_name = {f.name: f for f in fields(request_type)}
        assert by_name["taker_fee_bps"].default_factory is default_taker_bps
        assert by_name["maker_fee_bps"].default_factory is default_maker_bps

    def test_the_risk_cushion_uses_the_same_taker_fee(self) -> None:
        # the cash budget is deflated by this, so an optimistic fee approves
        # orders the account cannot actually afford
        assert RiskConfig().taker_fee_bps == default_taker_bps()


class TestFillStaysBackwardCompatible:
    def test_the_new_fields_are_optional(self) -> None:
        fill = Fill(
            order_id=OrderId("o1"),
            pair=PAIR,
            side=Side.BUY,
            ts=BASE,
            price=100.0,
            size=1.0,
            fee=0.1,
        )
        assert fill.taker_or_maker is None
        assert fill.fee_currency is None


def test_the_fake_market_carries_the_real_tick() -> None:
    # guards the fixture the rounding tests lean on
    assert FakeKrakenClient().market == MarketMeta(
        amount_step=Decimal("0.01"),
        price_step=TICK,
        min_amount=0.05,
        min_cost=5.0,
    )
