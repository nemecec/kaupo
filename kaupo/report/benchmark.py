"""Buy-and-hold benchmark for a run's equity curve.

Answers "did the strategy beat just holding?": the run's starting cash put
into the run's pair(s) at the start of the run window and never touched.
Frictionless on purpose — no fees, no slippage — so the line tracks the raw
close-price curve of the underlying.

Single-pair runs track that pair's close, normalized to ``starting_cash`` at
the close at-or-before the first equity snapshot. Portfolio runs (a ``pairs``
universe) split ``starting_cash`` equally across the pairs: each leg is bought
at its first in-window close (before that the slice is cash) and marked at the
last known close per snapshot timestamp — stale-price carry, the same
convention as the portfolio engine.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import CandleRow, RunRow

# fallbacks for run configs written before these keys existed (or by hand)
_DEFAULT_TIMEFRAME = "1h"
_DEFAULT_EXCHANGE = "kraken"
_DEFAULT_STARTING_CASH = 10_000.0


async def _closes_between(
    session: AsyncSession, pair: str, timeframe: str, exchange: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """(ts, close) ascending for the pair's candles with ts in [start, end]."""
    rows = await session.execute(
        select(CandleRow.ts, CandleRow.close)
        .where(
            CandleRow.exchange == exchange,
            CandleRow.pair == pair,
            CandleRow.timeframe == timeframe,
            CandleRow.ts >= start,
            CandleRow.ts <= end,
        )
        .order_by(CandleRow.ts)
    )
    return [(row.ts, row.close) for row in rows.all()]


async def _close_at_or_before(
    session: AsyncSession, pair: str, timeframe: str, exchange: str, ts: datetime
) -> tuple[datetime, float] | None:
    """(ts, close) of the pair's latest candle at-or-before ``ts``."""
    row = (
        await session.execute(
            select(CandleRow.ts, CandleRow.close)
            .where(
                CandleRow.exchange == exchange,
                CandleRow.pair == pair,
                CandleRow.timeframe == timeframe,
                CandleRow.ts <= ts,
            )
            .order_by(CandleRow.ts.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (row.ts, row.close)


def _carried(closes: list[tuple[datetime, float]], tss: list[datetime]) -> list[float | None]:
    """Close at-or-before each ts in ``tss``; None where ts precedes the first close.

    ``closes`` and ``tss`` must both be ascending. Runs are per-candle, so in
    practice every snapshot lands exactly on a candle close (1:1 alignment).
    """
    out: list[float | None] = []
    i = -1
    for ts in tss:
        while i + 1 < len(closes) and closes[i + 1][0] <= ts:
            i += 1
        out.append(closes[i][1] if i >= 0 else None)
    return out


async def _single_pair(
    session: AsyncSession,
    pair: str,
    timeframe: str,
    exchange: str,
    starting_cash: float,
    start: datetime,
    end: datetime,
    tss: list[datetime],
) -> list[tuple[datetime, float]]:
    """``starting_cash * close / close_at_start`` per snapshot ts."""
    anchor = await _close_at_or_before(session, pair, timeframe, exchange, start)
    closes = await _closes_between(session, pair, timeframe, exchange, start, end)
    if anchor is not None:
        closes = [anchor, *closes]  # anchor.ts <= start <= every in-window ts
    if not closes:
        return []
    base = closes[0][1]  # close at-or-before the first snapshot (or first in-window close)
    carried = _carried(closes, tss)
    return [
        (ts, starting_cash * close / base)
        for ts, close in zip(tss, carried, strict=True)
        if close is not None  # snapshot before the first known close
    ]


async def _portfolio(
    session: AsyncSession,
    pairs: list[str],
    timeframe: str,
    exchange: str,
    starting_cash: float,
    start: datetime,
    end: datetime,
    tss: list[datetime],
) -> list[tuple[datetime, float]]:
    """Equal-weight buy-and-hold: split cash equally, buy each leg at its first
    in-window close, then mark at the last known close per timestamp."""
    alloc = starting_cash / len(pairs)
    # (units, carried closes) per pair; None = no in-window candles, slice stays cash
    legs: list[tuple[float, list[float | None]] | None] = []
    for pair in pairs:
        closes = await _closes_between(session, pair, timeframe, exchange, start, end)
        if not closes:
            legs.append(None)
            continue
        units = alloc / closes[0][1]
        legs.append((units, _carried(closes, tss)))
    if all(leg is None for leg in legs):
        return []
    out: list[tuple[datetime, float]] = []
    for i, ts in enumerate(tss):
        total = 0.0
        for leg in legs:
            if leg is None:
                total += alloc
            else:
                units, carried = leg
                close = carried[i]
                total += alloc if close is None else units * close
        out.append((ts, total))
    return out


async def buy_and_hold_benchmark(
    session: AsyncSession,
    *,
    pairs: list[str],
    timeframe: str,
    exchange: str,
    starting_cash: float,
    tss: list[datetime],
) -> list[tuple[datetime, float]]:
    """(ts, value) benchmark aligned to the ascending snapshot timestamps ``tss``.

    Empty when no candles cover the window — never an error.
    """
    if not pairs or not tss:
        return []
    start, end = tss[0], tss[-1]
    if len(pairs) == 1:
        return await _single_pair(session, pairs[0], timeframe, exchange, starting_cash, start, end, tss)
    return await _portfolio(session, pairs, timeframe, exchange, starting_cash, start, end, tss)


def _universe(config: dict[str, Any]) -> list[str]:
    """Benchmark universe: the portfolio ``pairs`` list when present, else the single ``pair``."""
    pairs = config.get("pairs")
    if isinstance(pairs, list) and pairs:
        return [str(p) for p in pairs]
    pair = config.get("pair")
    return [pair] if isinstance(pair, str) and pair else []


async def run_benchmark(
    session: AsyncSession, run: RunRow, tss: list[datetime]
) -> list[tuple[datetime, float]]:
    """Buy-and-hold benchmark for a run, derived from its stored config."""
    if not tss:
        return []
    config = run.config or {}
    pairs = _universe(config)
    if not pairs:
        return []
    cash = config.get("starting_cash")
    return await buy_and_hold_benchmark(
        session,
        pairs=pairs,
        timeframe=str(config.get("timeframe", _DEFAULT_TIMEFRAME)),
        exchange=str(config.get("exchange", _DEFAULT_EXCHANGE)),
        starting_cash=float(cash) if isinstance(cash, int | float) else _DEFAULT_STARTING_CASH,
        tss=tss,
    )
