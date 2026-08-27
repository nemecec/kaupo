"""Parameter sweeps: one backtest per point of a parameter grid.

A sweep maps a parameter surface with one submission instead of one job
per point: the request carries a spec of param name -> list of values,
the grid is the cartesian product, and every point runs with an identical
configuration except the swept params (a swept value overrides the base
``params`` value at that point). Grid order is the declaration order of
the spec: nested loops over its keys with the LAST key varying fastest.

Each point persists as a normal runs row whose config carries a ``sweep``
marker tying it to the group. A point that fails (no candles in range, a
value the strategy params schema rejects, an engine error) degrades to an
error entry in the aggregation — the other points still run and the job
completes. Spec-level errors (unknown strategy, unknown param names) are
rejected at submit time and fail the job, never degrade.

Sweep and stability windows are mutually exclusive for now: a sweep of
stability slices may come later if wanted.
"""

import logging
from dataclasses import replace
from itertools import product
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.sdk.protocol import LoadedStrategy

log = logging.getLogger(__name__)

AnyBacktestRequest = BacktestRequest | PortfolioBacktestRequest

MAX_SWEEP_POINTS = 50


def sweep_size(spec: dict[str, list[Any]]) -> int:
    """The number of grid points: the product of the list lengths."""
    size = 1
    for values in spec.values():
        size *= len(values)
    return size


def validate_sweep_spec(spec: dict[str, list[Any]]) -> None:
    """The strategy-independent spec rules; ValueError on any violation."""
    if not spec:
        raise ValueError("sweep must name at least one param")
    for key, values in spec.items():
        if not values:
            raise ValueError(f"sweep param {key!r} needs at least one value")
        for value in values:
            if value is None or isinstance(value, (list, dict)):
                raise ValueError(
                    f"sweep param {key!r} values must be scalars (str, int, float, bool), got {value!r}"
                )
    size = sweep_size(spec)
    if size > MAX_SWEEP_POINTS:
        raise ValueError(f"sweep expands to {size} points; the cap is {MAX_SWEEP_POINTS}")


def validate_sweep_keys(
    strategy: LoadedStrategy, base_params: dict[str, Any], spec: dict[str, list[Any]]
) -> None:
    """Reject spec keys the strategy params schema does not know.

    Reuses the strategy's own validation: the first point's merged params
    go through the same create() path a run would, so unknown keys (and a
    bad first value) raise exactly the error a bad --param would. Later
    values are only checked when their point runs — a bad one degrades to
    an error entry instead of failing the submission.
    """
    first_point = expand_sweep(spec)[0]
    try:
        strategy.create({**base_params, **first_point})
    except ValueError as exc:
        raise ValueError(f"invalid sweep for strategy {strategy.id!r}: {exc}") from exc


def expand_sweep(spec: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """One params dict per grid point, in declaration order: nested loops
    over the spec's keys with the LAST key varying fastest."""
    keys = list(spec)
    return [dict(zip(keys, combo, strict=True)) for combo in product(*(spec[key] for key in keys))]


def sweep_marker(group: str, point: dict[str, Any]) -> dict[str, Any]:
    """The config marker of a sweep run: which group, which grid point."""
    return {"group": group, "point": point}


async def _run_one(
    request: AnyBacktestRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[str, dict[str, Any]]:
    if isinstance(request, PortfolioBacktestRequest):
        run_id, _, metrics = await run_portfolio_backtest(request, sessionmaker)
    else:
        run_id, _, metrics = await run_backtest(request, sessionmaker)
    return str(run_id), metrics


async def run_sweep(
    request: AnyBacktestRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    group: str,
    spec: dict[str, list[Any]],
) -> tuple[str | None, dict[str, Any]]:
    """Run the request once per grid point, sequentially; never raises on a point failure.

    Returns (run id of the first successful point or None when every point
    failed, {"sweep": [{"params", "run_id"?, "metrics"?, "error"?}]}). The
    entries follow the grid order (declaration order, last key fastest).
    """
    entries: list[dict[str, Any]] = []
    first_run_id: str | None = None
    for point in expand_sweep(spec):
        entry: dict[str, Any] = {"params": point}
        point_request = replace(
            request,
            params={**request.params, **point},
            sweep=sweep_marker(group, point),
        )
        try:
            run_id, metrics = await _run_one(point_request, sessionmaker)
        except Exception as exc:  # a bad point degrades; it never fails the job
            log.warning("Sweep point %s failed: %s", point, exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            entry["run_id"] = run_id
            entry["metrics"] = metrics
            if first_run_id is None:
                first_run_id = run_id
        entries.append(entry)
    return first_run_id, {"sweep": entries}
