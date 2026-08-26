"""Turn a validated BacktestIn body into an executable backtest request.

Shared by the API (validate at submit time) and the backtest worker
(rebuild from the queued payload), so both apply exactly the same lint
gate, strategy loading, and request validation. Every expected failure
raises ValueError (or a subclass); the API maps them to 4xx responses,
the worker fails the job with the message.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.portfolio import PortfolioBacktestRequest
from kaupo.backtest.run import BacktestRequest, backtest_risk_config
from kaupo.domain import Pair, Timeframe
from kaupo.sdk.loader import load_strategies
from kaupo.sdk.protocol import LoadedStrategy


class LintViolationsError(ValueError):
    """The strategies directory has determinism violations."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("strategies have determinism violations")


class UnknownStrategyError(ValueError):
    """The requested strategy id is not in the loaded strategies."""


def lint_and_load_strategies(directory: Path) -> dict[str, LoadedStrategy]:
    """Refuse directories with determinism violations, then load the strategies.

    FileNotFoundError propagates (a misconfigured deployment, not a bad
    request).
    """
    from kaupo.sdk.lint import lint_directory

    violations = lint_directory(directory)
    if violations:
        raise LintViolationsError([str(v) for v in violations])
    return load_strategies(directory)


def build_backtest_request(
    body: BacktestIn,
    strategies: dict[str, LoadedStrategy],
) -> BacktestRequest | PortfolioBacktestRequest:
    """Build the executable request; ValueError on any invalid input."""
    loaded = strategies.get(body.strategy)
    if loaded is None:
        raise UnknownStrategyError(f"unknown strategy {body.strategy!r}; available: {sorted(strategies)}")

    def aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    end = aware(body.end) if body.end else datetime.now(UTC)
    start = aware(body.start) if body.start else end - timedelta(days=body.days)

    risk = backtest_risk_config(
        max_position_quote=body.max_position_quote,
        max_gross_exposure_quote=body.max_gross_exposure_quote,
        max_daily_loss_quote=body.max_daily_loss_quote,
    )
    timeframe = Timeframe.parse(body.timeframe)
    if body.pairs is not None:
        if not loaded.is_portfolio:
            raise ValueError(f"strategy {body.strategy!r} is not a portfolio strategy; pass pair")
        return PortfolioBacktestRequest(
            strategy=loaded,
            params=body.params,
            pairs=[Pair.parse(p) for p in body.pairs],
            timeframe=timeframe,
            start=start,
            end=end,
            starting_cash=body.starting_cash,
            exchange=body.exchange,
            risk=risk,
        )
    if loaded.is_portfolio:
        raise ValueError(f"strategy {body.strategy!r} is a portfolio strategy; pass pairs")
    assert body.pair is not None  # the schema guarantees exactly one of pair/pairs
    return BacktestRequest(
        strategy=loaded,
        params=body.params,
        pair=Pair.parse(body.pair),
        timeframe=timeframe,
        start=start,
        end=end,
        starting_cash=body.starting_cash,
        exchange=body.exchange,
        risk=risk,
    )
