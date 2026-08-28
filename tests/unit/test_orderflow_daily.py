"""OrderflowDailyOut schema shape; GET /api/v1/orderflow/daily validation (no DB)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from kaupo.api.deps import Principal
from kaupo.api.routes.data import orderflow_daily
from kaupo.api.schemas import OrderflowDailyOut

BASE = datetime(2026, 1, 1, tzinfo=UTC)


class TestOrderflowDailyOut:
    def test_round_trips_the_table_columns(self) -> None:
        row = OrderflowDailyOut(
            exchange="kraken",
            pair="BTC/EUR",
            day=date(2026, 8, 27),
            trade_count=10,
            buy_count=6,
            sell_count=4,
            buy_volume=3.0,
            sell_volume=2.0,
            max_trade_size=1.5,
            book_snapshots=24,
            spread_mean_bps=5.0,
            spread_max_bps=9.0,
        )
        assert row.model_dump() == {
            "exchange": "kraken",
            "pair": "BTC/EUR",
            "day": date(2026, 8, 27),
            "trade_count": 10,
            "buy_count": 6,
            "sell_count": 4,
            "buy_volume": 3.0,
            "sell_volume": 2.0,
            "max_trade_size": 1.5,
            "book_snapshots": 24,
            "spread_mean_bps": 5.0,
            "spread_max_bps": 9.0,
        }

    def test_spread_fields_are_nullable(self) -> None:
        # days without book snapshots carry null spread statistics
        row = OrderflowDailyOut(
            exchange="kraken",
            pair="BTC/EUR",
            day=date(2026, 8, 27),
            trade_count=10,
            buy_count=6,
            sell_count=4,
            buy_volume=3.0,
            sell_volume=2.0,
            max_trade_size=1.5,
            book_snapshots=0,
            spread_mean_bps=None,
            spread_max_bps=None,
        )
        assert row.spread_mean_bps is None
        assert row.spread_max_bps is None

    def test_required_fields_have_no_defaults(self) -> None:
        with pytest.raises(ValidationError):
            OrderflowDailyOut.model_validate({"exchange": "kraken", "pair": "BTC/EUR"})


async def test_orderflow_daily_endpoint_rejects_a_bad_pair() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await orderflow_daily(
            Principal(admin=False),
            None,  # type: ignore[arg-type]  # unused: pair parsing fails first
            pair="bogus",
            start=BASE.date(),
            end=(BASE + timedelta(days=1)).date(),
            limit=100,
            exchange="kraken",
        )
    assert exc_info.value.status_code == 422
