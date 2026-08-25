"""Per-pair metrics grouping and portfolio attribution."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.backtest.metrics import compute_metrics, round_trips
from kaupo.domain import Fill, OrderId, Pair, Side, Timeframe

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
ADA = Pair.parse("ADA/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def fill(pair: Pair, side: Side, price: float, size: float, fee: float = 0.0, i: int = 0) -> Fill:
    return Fill(order_id=OrderId(f"o{i}"), pair=pair, side=side, ts=BASE, price=price, size=size, fee=fee)


def curve(values: list[float]) -> list[tuple[datetime, float]]:
    return [(BASE + timedelta(hours=i), v) for i, v in enumerate(values)]


def test_round_trips_group_per_pair_from_an_interleaved_stream() -> None:
    # interleaved fills of two pairs; grouping must not cross-pair FIFO
    fills = [
        fill(BTC, Side.BUY, 100, 1, i=0),
        fill(SOL, Side.BUY, 50, 2, i=1),
        fill(BTC, Side.SELL, 110, 1, i=2),
        fill(SOL, Side.SELL, 40, 2, i=3),
    ]
    m = compute_metrics(curve([100, 100, 100]), fills, Timeframe.H1, 100)
    assert m["num_round_trips"] == 2
    btc_trips = round_trips([f for f in fills if f.pair == BTC])
    sol_trips = round_trips([f for f in fills if f.pair == SOL])
    assert btc_trips[0].pnl == pytest.approx(10.0)
    assert sol_trips[0].pnl == pytest.approx(-20.0)
    assert m["win_rate_pct"] == 50.0


def test_no_universe_keeps_the_single_pair_key_set() -> None:
    m = compute_metrics(curve([100, 101, 102]), [], Timeframe.H1, 100)
    assert "universe" not in m
    assert "per_pair" not in m


def test_portfolio_attribution_per_pair() -> None:
    fills = [
        fill(BTC, Side.BUY, 100, 1, fee=1, i=0),
        fill(BTC, Side.SELL, 110, 1, fee=1, i=1),
        fill(SOL, Side.BUY, 50, 2, fee=1, i=2),
        fill(SOL, Side.SELL, 40, 2, fee=1, i=3),
    ]
    m = compute_metrics(
        curve([100, 100, 100]),
        fills,
        Timeframe.H1,
        100,
        universe=["ADA/EUR", "BTC/EUR", "SOL/EUR"],
    )
    assert m["universe"] == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]
    per_pair = m["per_pair"]
    assert list(per_pair) == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]

    btc = per_pair["BTC/EUR"]
    assert btc["round_trips"] == 1
    assert btc["realized_pnl"] == pytest.approx(8.0)  # 10 minus both fees
    assert btc["fees_paid"] == pytest.approx(2.0)
    assert btc["win_rate_pct"] == 100.0

    sol = per_pair["SOL/EUR"]
    assert sol["round_trips"] == 1
    assert sol["realized_pnl"] == pytest.approx(-22.0)  # -20 minus both fees
    assert sol["fees_paid"] == pytest.approx(2.0)
    assert sol["win_rate_pct"] == 0.0

    ada = per_pair["ADA/EUR"]  # universe pair with no activity: zeros
    assert ada == {"realized_pnl": 0.0, "fees_paid": 0.0, "round_trips": 0, "win_rate_pct": None}
