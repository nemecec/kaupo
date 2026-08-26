"""Stability windows: re-run a backtest over K equal time slices of [start, end].

A candidate that only works from one start date is probably overfitted.
Stability windows make that check mechanical: after the full-window run,
the same request runs over each slice (with the usual lookback prefill
before the slice start, exactly what a live run starting then would see),
and every slice persists as a normal runs row whose config carries a
``stability`` marker tying it to the group. A slice that fails (e.g. no
candles in its range) degrades to an error entry in the aggregation — the
full-window run stays the primary artifact. The platform reports the
per-window metrics; pass/fail judgement stays with the reviewer.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import BacktestRequest, run_backtest
from kaupo.domain import RunId

log = logging.getLogger(__name__)

AnyBacktestRequest = BacktestRequest | PortfolioBacktestRequest


def compute_slices(start: datetime, end: datetime, windows: int) -> list[tuple[datetime, datetime]]:
    """K equal half-open slices covering [start, end]: slice i is
    [start + i*(end-start)/K, start + (i+1)*(end-start)/K). Boundaries are
    shared (no gaps, no overlaps); the last slice ends exactly at ``end``
    so rounding never drifts the coverage."""
    step = (end - start) / windows
    if step <= timedelta(0):
        raise ValueError(f"{windows} stability windows do not fit into the {end - start} backtest span")
    return [(start + step * i, end if i == windows - 1 else start + step * (i + 1)) for i in range(windows)]


def stability_marker(group: str, window: int | str, of: int) -> dict[str, Any]:
    """The config marker of a stability run: which group, which window ("full" or the slice index)."""
    return {"group": group, "window": window, "of": of}


async def _run_one(
    request: AnyBacktestRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[RunId, dict[str, Any]]:
    if isinstance(request, PortfolioBacktestRequest):
        run_id, _, metrics = await run_portfolio_backtest(request, sessionmaker)
    else:
        run_id, _, metrics = await run_backtest(request, sessionmaker)
    return run_id, metrics


async def run_stability_slices(
    request: AnyBacktestRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    group: str,
    windows: int,
) -> dict[str, Any]:
    """Run the request over each slice of [start, end]; never raises.

    Returns {"windows": K, "slices": [{"window", "start", "end", "run_id"?,
    "metrics"?, "error"?}]}. Slices are always persisted (auditability),
    even when the full-window request ran with persist=False.
    """
    try:
        slices = compute_slices(request.start, request.end, windows)
    except ValueError as exc:
        return {"windows": windows, "slices": [], "error": str(exc)}
    entries: list[dict[str, Any]] = []
    for i, (slice_start, slice_end) in enumerate(slices):
        entry: dict[str, Any] = {
            "window": i,
            "start": slice_start.isoformat(),
            "end": slice_end.isoformat(),
        }
        slice_request = replace(
            request,
            start=slice_start,
            end=slice_end,
            persist=True,
            stability=stability_marker(group, i, windows),
        )
        try:
            run_id, metrics = await _run_one(slice_request, sessionmaker)
        except Exception as exc:  # a bad slice degrades; it never fails the job
            log.warning("Stability window %d/%d failed: %s", i, windows, exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            entry["run_id"] = str(run_id)
            entry["metrics"] = metrics
        entries.append(entry)
    return {"windows": windows, "slices": entries}
