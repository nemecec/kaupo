from datetime import UTC, datetime, timedelta

import pytest

from scripts.pair_quality import (
    BENFORD_PROBS,
    Trade,
    benford_chi2,
    benford_counts,
    benford_distribution,
    buy_sell_count_ratio,
    first_digit,
    is_low_confidence,
    is_round_size,
    round_size_shares,
    round_to_sig,
    weekend_volume_share,
)

BASE = datetime(2026, 8, 24, tzinfo=UTC)  # a Monday


def trade(amount: float, price: float = 100.0, day_offset: int = 0, side: str | None = "buy") -> Trade:
    return Trade(
        ts=BASE + timedelta(days=day_offset),
        price=price,
        amount=amount,
        side=side,
        trade_id=None,
    )


class TestFirstDigit:
    @pytest.mark.parametrize(
        ("x", "digit"),
        [(1.0, 1), (10.0, 1), (0.03, 3), (9.99, 9), (0.5, 5), (250.0, 2)],
    )
    def test_digits(self, x: float, digit: int) -> None:
        assert first_digit(x) == digit

    @pytest.mark.parametrize("x", [0.0, -1.0, float("inf"), float("nan")])
    def test_rejects_non_positive(self, x: float) -> None:
        with pytest.raises(ValueError, match="positive finite"):
            first_digit(x)


class TestBenford:
    def test_counts(self) -> None:
        counts = benford_counts([1.0, 1.5, 0.02, 9.9, 0.0, -3.0])
        assert counts == [2, 1, 0, 0, 0, 0, 0, 0, 1]

    def test_chi2_zero_for_benford_sample(self) -> None:
        n = 100_000
        counts = [round(n * p) for p in BENFORD_PROBS.values()]
        assert benford_chi2(counts) == pytest.approx(0.0, abs=1.0)

    def test_chi2_large_for_uniform_sample(self) -> None:
        counts = [10_000] * 9
        assert benford_chi2(counts) > 1_000

    def test_chi2_nan_for_empty(self) -> None:
        assert benford_chi2([0] * 9) != benford_chi2([0] * 9)  # NaN

    def test_distribution_sums_to_one(self) -> None:
        dist = benford_distribution([5, 3, 2, 0, 0, 0, 0, 0, 0])
        assert sum(dist.values()) == pytest.approx(1.0)
        assert dist[1] == pytest.approx(5 / 10)


class TestRoundSize:
    @pytest.mark.parametrize(
        "amount",
        [1.0, 0.05, 0.25, 100.0, 1.5, 0.003, 2.5e-5],
    )
    def test_round(self, amount: float) -> None:
        assert is_round_size(amount)

    @pytest.mark.parametrize(
        "amount",
        [0.251, 1.53, 0.123, 1.005, 0.0, -1.0, float("nan")],
    )
    def test_not_round(self, amount: float) -> None:
        assert not is_round_size(amount)

    def test_round_to_sig(self) -> None:
        assert round_to_sig(0.251, 2) == 0.25
        assert round_to_sig(1234.0, 1) == 1000.0

    def test_shares(self) -> None:
        trades = [trade(0.05), trade(0.0513), trade(1.0), trade(1.234)]
        by_count, by_volume = round_size_shares(trades) or (None, None)
        assert by_count == pytest.approx(0.5)
        # price 100: round volume = (0.05 + 1.0) * 100, total = sum * 100
        assert by_volume == pytest.approx(1.05 / (0.05 + 0.0513 + 1.0 + 1.234))

    def test_shares_empty(self) -> None:
        assert round_size_shares([]) is None


class TestWeekendShare:
    def test_weekend_share(self) -> None:
        # BASE is Monday; day_offset 5 is Saturday, 6 is Sunday
        trades = [trade(1.0, day_offset=d) for d in range(7)]
        share = weekend_volume_share(trades)
        assert share == pytest.approx(2 / 7)

    def test_none_when_empty(self) -> None:
        assert weekend_volume_share([]) is None


class TestLowConfidence:
    def test_flag(self) -> None:
        assert is_low_confidence(1.8, 7.0)
        assert not is_low_confidence(7.0, 7.0)
        # first trade lands minutes after the window start: no false flag
        assert not is_low_confidence(6.97, 7.0)
        assert is_low_confidence(6.9, 7.0)


class TestBuySellRatio:
    def test_ratio(self) -> None:
        trades = [trade(1.0, side="buy"), trade(1.0, side="buy"), trade(1.0, side="sell")]
        assert buy_sell_count_ratio(trades) == pytest.approx(2.0)

    def test_none_without_sides(self) -> None:
        assert buy_sell_count_ratio([trade(1.0, side=None)]) is None
