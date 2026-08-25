"""Portfolio request validation: one quote, no empty/duplicate universe."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.domain import Pair, Timeframe
from kaupo.sdk.protocol import LoadedStrategy, PortfolioStrategyBase

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _Dummy(PortfolioStrategyBase):
    id = "dummy"

    def on_candle(self, ctx):
        return []


STRATEGY = LoadedStrategy(id="dummy", cls=_Dummy, source_hash="x" * 64, path="/dev/null")


def request(pairs: list[Pair]) -> PortfolioBacktestRequest:
    return PortfolioBacktestRequest(
        strategy=STRATEGY,
        params={},
        pairs=pairs,
        timeframe=Timeframe.H1,
        start=BASE,
        end=BASE.replace(day=2),
    )


class TestPortfolioBacktestRequest:
    def test_valid_universe_is_sorted_canonically(self) -> None:
        req = request([SOL, BTC])
        assert req.pairs == [BTC, SOL]

    def test_mixed_quotes_rejected(self) -> None:
        with pytest.raises(ValueError, match="one quote currency"):
            request([BTC, Pair.parse("SOL/USD")])

    def test_empty_universe_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2 pairs"):
            request([])

    def test_single_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2 pairs"):
            request([BTC])

    def test_duplicate_pairs_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate pairs"):
            request([BTC, SOL, BTC])


class TestBacktestInSchema:
    def test_pair_or_pairs_required(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of pair or pairs"):
            BacktestIn(strategy="s")

    def test_pair_and_pairs_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of pair or pairs"):
            BacktestIn(strategy="s", pair="BTC/EUR", pairs=["BTC/EUR", "SOL/EUR"])

    def test_pairs_needs_two_entries(self) -> None:
        with pytest.raises(ValidationError, match="at least 2 entries"):
            BacktestIn(strategy="s", pairs=["BTC/EUR"])

    def test_pair_only_still_valid(self) -> None:
        body = BacktestIn(strategy="s", pair="BTC/EUR")
        assert body.pairs is None

    def test_pairs_valid(self) -> None:
        body = BacktestIn(strategy="s", pairs=["BTC/EUR", "SOL/EUR"])
        assert body.pair is None
