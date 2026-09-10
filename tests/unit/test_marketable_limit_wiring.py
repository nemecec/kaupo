"""The wiring contract for the marketable-limit mode (kaupo#36 spec section 5).

Two rules matter more than the venue logic itself:

- shadow runs and the rolling-origin re-backtests of those runs must use the
  SAME mode, or the triage compares two different venue models and its
  verdicts are noise;
- an ad-hoc API backtest must keep the legacy maker model unless it asks
  otherwise, so every recorded number the promotion gates read stays
  comparable.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.backtest.run import BacktestRequest
from kaupo.core import live_runner, runner
from kaupo.domain import Pair, Timeframe
from kaupo.report import rolling
from kaupo.sdk.loader import load_strategies
from kaupo.venues import paper
from kaupo.venues.paper import DEFAULT_MARKETABLE_LIMIT, LIVE_MIRROR_MARKETABLE_LIMIT

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "strategies"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class TestShadowAndRollingMoveTogether:
    """One constant, imported by every module that must agree with the live venue.

    A guard, not a proof: the behavioural proof that the shadow runner and the
    rolling re-backtest actually use it lives in
    ``tests/integration/test_shadow_db.py`` (the recorded run config) and
    ``tests/unit/test_rolling_origin.py`` (the request the report builds).
    """

    def test_every_module_that_must_agree_exposes_the_same_value(self) -> None:
        assert (
            runner.LIVE_MIRROR_MARKETABLE_LIMIT
            == rolling.LIVE_MIRROR_MARKETABLE_LIMIT
            == live_runner.LIVE_MIRROR_MARKETABLE_LIMIT
            == paper.LIVE_MIRROR_MARKETABLE_LIMIT
        )

    def test_it_is_the_mode_that_mirrors_post_only(self) -> None:
        # KrakenVenue posts every limit post-only, so a marketable limit is
        # rejected and never fills; "skip" is that venue in the paper model
        assert LIVE_MIRROR_MARKETABLE_LIMIT == "skip"
        assert LIVE_MIRROR_MARKETABLE_LIMIT != DEFAULT_MARKETABLE_LIMIT


class TestBacktestApiDefaultsToLegacy:
    def _request(self, **overrides: object) -> BacktestRequest | PortfolioBacktestRequest:
        body = BacktestIn(
            strategy="regime-switch",
            pair="BTC/EUR",
            timeframe="1h",
            start=BASE,
            end=BASE + timedelta(hours=48),
            **overrides,  # type: ignore[arg-type]
        )
        return build_backtest_request(body, lint_and_load_strategies(EXAMPLES_DIR))

    def test_an_unset_field_means_legacy_maker(self) -> None:
        body = BacktestIn(strategy="regime-switch", pair="BTC/EUR", timeframe="1h")
        assert body.marketable_limit is None  # the wire default says nothing

        request = self._request()
        assert request.marketable_limit == "maker"  # the request says legacy

    @pytest.mark.parametrize("mode", ["maker", "taker", "skip"])
    def test_every_mode_can_be_asked_for(self, mode: str) -> None:
        assert self._request(marketable_limit=mode).marketable_limit == mode

    def test_an_unknown_mode_is_rejected_at_the_schema(self) -> None:
        with pytest.raises(ValidationError):
            BacktestIn(
                strategy="regime-switch",
                pair="BTC/EUR",
                timeframe="1h",
                marketable_limit="post-only",  # type: ignore[arg-type]
            )

    def test_a_portfolio_backtest_carries_the_mode_too(self) -> None:
        body = BacktestIn(
            strategy="momentum-rotation",
            pairs=["BTC/EUR", "SOL/EUR"],
            timeframe="1h",
            marketable_limit="skip",
        )
        request = build_backtest_request(body, lint_and_load_strategies(EXAMPLES_DIR))
        assert isinstance(request, PortfolioBacktestRequest)
        assert request.marketable_limit == "skip"

    def test_the_field_survives_the_job_queue_round_trip(self) -> None:
        # the worker rebuilds the request from the queued JSON payload
        body = BacktestIn(strategy="regime-switch", pair="BTC/EUR", timeframe="1h", marketable_limit="taker")
        restored = BacktestIn.model_validate(body.model_dump(mode="json"))
        assert restored == body
        assert restored.marketable_limit == "taker"


class TestRequestDefaults:
    """The dataclass default is legacy, so every unnamed construction site is safe.

    The CLI builds its requests directly and names no mode, so `kaupo backtest`
    keeps the legacy model through this default.
    """

    def test_both_request_types_default_to_maker(self) -> None:
        loaded = load_strategies(EXAMPLES_DIR)
        single = BacktestRequest(
            strategy=loaded["regime-switch"],
            params={},
            pair=Pair.parse("BTC/EUR"),
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=1),
        )
        portfolio = PortfolioBacktestRequest(
            strategy=loaded["momentum-rotation"],
            params={},
            pairs=[Pair.parse("BTC/EUR"), Pair.parse("SOL/EUR")],
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=1),
        )
        assert single.marketable_limit == DEFAULT_MARKETABLE_LIMIT == "maker"
        assert portfolio.marketable_limit == DEFAULT_MARKETABLE_LIMIT
