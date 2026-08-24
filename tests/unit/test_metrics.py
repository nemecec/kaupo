from datetime import UTC, datetime, timedelta

import pytest

from kaupo.backtest.metrics import compute_metrics, round_trips
from kaupo.domain import Fill, OrderId, Pair, Side, Timeframe

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def fill(side: Side, price: float, size: float, fee: float = 0.0, i: int = 0) -> Fill:
    return Fill(order_id=OrderId(f"o{i}"), pair=PAIR, side=side, ts=BASE, price=price, size=size, fee=fee)


class TestRoundTrips:
    def test_simple_trip(self) -> None:
        trips = round_trips([fill(Side.BUY, 100, 1), fill(Side.SELL, 110, 1, i=1)])
        assert len(trips) == 1
        assert trips[0].pnl == pytest.approx(10.0)

    def test_loss(self) -> None:
        trips = round_trips([fill(Side.BUY, 100, 1), fill(Side.SELL, 90, 1, i=1)])
        assert trips[0].pnl == pytest.approx(-10.0)

    def test_partial_sell(self) -> None:
        trips = round_trips([fill(Side.BUY, 100, 2), fill(Side.SELL, 110, 1, i=1)])
        assert len(trips) == 1
        assert trips[0].pnl == pytest.approx(10.0)

    def test_fees_included(self) -> None:
        trips = round_trips([fill(Side.BUY, 100, 1, fee=1), fill(Side.SELL, 110, 1, fee=1, i=1)])
        # cost basis 101, sell fee 1 -> pnl 8
        assert trips[0].pnl == pytest.approx(8.0)

    def test_sell_without_position_ignored(self) -> None:
        assert round_trips([fill(Side.SELL, 100, 1)]) == []


class TestComputeMetrics:
    def equity_curve(self, values: list[float]) -> list[tuple[datetime, float]]:
        return [(BASE + timedelta(hours=i), v) for i, v in enumerate(values)]

    def test_growing_curve(self) -> None:
        m = compute_metrics(self.equity_curve([100, 101, 102, 103, 104]), [], Timeframe.H1, 100)
        assert m["total_return_pct"] == 4.0
        assert m["sharpe"] > 0
        assert m["max_drawdown_pct"] == 0.0
        assert m["final_equity"] == 104.0

    def test_drawdown(self) -> None:
        m = compute_metrics(self.equity_curve([100, 110, 88, 99, 110]), [], Timeframe.H1, 100)
        assert m["max_drawdown_pct"] == pytest.approx(-20.0)

    def test_trade_stats(self) -> None:
        fills = [
            fill(Side.BUY, 100, 1),
            fill(Side.SELL, 110, 1, i=1),
            fill(Side.BUY, 100, 1, i=2),
            fill(Side.SELL, 90, 1, i=3),
        ]
        m = compute_metrics(self.equity_curve([100, 100, 100]), fills, Timeframe.H1, 100)
        assert m["num_round_trips"] == 2
        assert m["win_rate_pct"] == 50.0
        assert m["avg_win"] == 10.0
        assert m["avg_loss"] == -10.0
        assert m["profit_factor"] == 1.0

    def test_too_little_data(self) -> None:
        m = compute_metrics(self.equity_curve([100]), [], Timeframe.H1, 100)
        assert "error" in m


def test_metrics_with_zero_in_curve() -> None:
    # a zero in the curve: division produces inf/nan — metrics must not crash
    m = compute_metrics(
        [(BASE + timedelta(hours=i), v) for i, v in enumerate([100, 0, 50, 100])],
        [],
        Timeframe.H1,
        100,
    )
    assert "sharpe" in m
    assert m["max_drawdown_pct"] == -100.0


def test_metrics_remaining_keys() -> None:
    fills = [fill(Side.BUY, 100, 1, fee=1), fill(Side.SELL, 110, 1, fee=1, i=1)]
    curve = [(BASE + timedelta(hours=i), v) for i, v in enumerate([100, 105, 108])]
    m = compute_metrics(curve, fills, Timeframe.H1, 100)
    assert m["total_fees"] == 2.0
    assert m["days"] > 0
    assert "cagr_pct" in m
    assert "sortino" in m
    assert m["risk_rejections"] == 0
