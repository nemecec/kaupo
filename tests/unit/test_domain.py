from datetime import UTC, datetime

import pytest

from kaupo.domain import OrderIntent, OrderType, Pair, Side, Timeframe


class TestTimeframe:
    def test_parse(self) -> None:
        assert Timeframe.parse("1h") is Timeframe.H1
        assert Timeframe.parse("1d") is Timeframe.D1

    def test_parse_invalid(self) -> None:
        with pytest.raises(ValueError, match="Unknown timeframe"):
            Timeframe.parse("7h")

    def test_seconds_and_periods(self) -> None:
        assert Timeframe.H1.seconds == 3600
        assert Timeframe.D1.periods_per_year == pytest.approx(365.25)


class TestPair:
    def test_parse(self) -> None:
        p = Pair.parse("btc/eur")
        assert p.base == "BTC"
        assert p.quote == "EUR"
        assert str(p) == "BTC/EUR"

    def test_parse_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid pair"):
            Pair.parse("BTCEUR")


class TestOrderIntent:
    def test_market_ok(self) -> None:
        intent = OrderIntent(pair=Pair.parse("BTC/EUR"), side=Side.BUY, size=0.1)
        assert intent.order_type is OrderType.MARKET

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            OrderIntent(pair=Pair.parse("BTC/EUR"), side=Side.BUY, size=-1)

    def test_limit_requires_price(self) -> None:
        with pytest.raises(ValueError, match="limit_price"):
            OrderIntent(
                pair=Pair.parse("BTC/EUR"),
                side=Side.BUY,
                size=0.1,
                order_type=OrderType.LIMIT,
            )


def test_utc_now_is_aware() -> None:
    from kaupo.domain import utc_now

    now = utc_now()
    assert now.tzinfo is UTC
    assert isinstance(now, datetime)
