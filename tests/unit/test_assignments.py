"""Assignment universe validation: normalize_universe + the API schemas (no DB)."""

import pytest
from pydantic import ValidationError

from kaupo.api.schemas import AssignmentIn, AssignmentUpdate
from kaupo.data.assignments import normalize_universe


class TestNormalizeUniverse:
    def test_canonical_sorted_order_and_normalization(self) -> None:
        assert normalize_universe(["sol/eur", "BTC/EUR", "ada/eur"]) == ["ADA/EUR", "BTC/EUR", "SOL/EUR"]

    def test_two_pairs_is_the_minimum(self) -> None:
        assert normalize_universe(["SOL/EUR", "BTC/EUR"]) == ["BTC/EUR", "SOL/EUR"]

    def test_single_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2 pairs"):
            normalize_universe(["BTC/EUR"])

    def test_duplicate_pairs_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate pairs"):
            normalize_universe(["BTC/EUR", "SOL/EUR", "btc/eur"])

    def test_mixed_quotes_rejected(self) -> None:
        with pytest.raises(ValueError, match="one quote currency"):
            normalize_universe(["BTC/EUR", "SOL/USD"])

    def test_invalid_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid pair"):
            normalize_universe(["BTC/EUR", "SOLEUR"])


class TestAssignmentInSchema:
    def test_pair_or_pairs_required(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of pair or pairs"):
            AssignmentIn(strategy_id="s", timeframe="1h")

    def test_pair_and_pairs_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of pair or pairs"):
            AssignmentIn(strategy_id="s", timeframe="1h", pair="BTC/EUR", pairs=["BTC/EUR", "SOL/EUR"])

    def test_pairs_needs_two_entries(self) -> None:
        with pytest.raises(ValidationError, match="at least 2 entries"):
            AssignmentIn(strategy_id="s", timeframe="1h", pairs=["BTC/EUR"])

    def test_pair_only_still_valid(self) -> None:
        body = AssignmentIn(strategy_id="s", timeframe="1h", pair="BTC/EUR")
        assert body.pairs is None

    def test_pairs_valid(self) -> None:
        body = AssignmentIn(strategy_id="s", timeframe="1h", pairs=["BTC/EUR", "SOL/EUR"])
        assert body.pair is None


class TestAssignmentUpdateSchema:
    def test_pair_and_pairs_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="at most one of pair or pairs"):
            AssignmentUpdate(pair="BTC/EUR", pairs=["BTC/EUR", "SOL/EUR"])

    def test_pairs_needs_two_entries(self) -> None:
        with pytest.raises(ValidationError, match="at least 2 entries"):
            AssignmentUpdate(pairs=["BTC/EUR"])

    def test_pairs_valid(self) -> None:
        body = AssignmentUpdate(pairs=["BTC/EUR", "SOL/EUR"])
        assert body.pair is None
