"""Daily machine-readable performance reports — the agent feedback artifact."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.metrics import open_position, round_trips
from kaupo.db.models import EquitySnapshotRow, FillRow, ReportRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Fill, OrderId, Pair, Side, new_id, utc_now


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _quote_currency(run: RunRow) -> str | None:
    """Read the ledger denomination from a single pair or portfolio universe."""
    config = run.config or {}
    pairs = config.get("pairs")
    if pairs is None:
        pair = config.get("pair")
        pairs = pair.split(",") if isinstance(pair, str) else []
    if not isinstance(pairs, list) or not pairs:
        return None
    try:
        quotes = {Pair.parse(pair).quote for pair in pairs if isinstance(pair, str)}
        if len(quotes) == 1 and all(isinstance(pair, str) for pair in pairs):
            return quotes.pop()
    except ValueError:
        pass
    return None


def _totals(run_reports: list[dict[str, Any]]) -> dict[str, Any]:
    active = [r for r in run_reports if r.get("active")]
    currencies = sorted(
        {r["quote_currency"] for r in active if r["quote_currency"] is not None}
        | {currency for r in active for currency in r.get("fees_by_currency", {})}
    )
    by_currency = {}
    for currency in currencies:
        group = [r for r in active if r["quote_currency"] == currency]
        by_currency[currency] = {
            "active_runs": len(group),
            "total_pnl": float(
                sum((Decimal(str(r["pnl"])) for r in group if r.get("pnl") is not None), Decimal(0))
            ),
            "total_fills": sum(r.get("num_fills", 0) for r in group),
            "total_fees": float(
                sum(
                    (Decimal(str(r.get("fees_by_currency", {}).get(currency, 0.0))) for r in active),
                    Decimal(0),
                )
            ),
        }
    unclassified = sum(r["quote_currency"] is None or r.get("unclassified_fee_fills", 0) > 0 for r in active)
    currency = currencies[0] if len(currencies) == 1 and not unclassified else None
    # Keep scalar amounts for single-currency consumers, but never sum
    # different or unknown denominations. No activity retains zero totals.
    return {
        "num_runs": len(run_reports),
        "active_runs": len(active),
        "total_fills": sum(r.get("num_fills", 0) for r in active),
        "quote_currency": currency,
        "total_pnl": by_currency[currency]["total_pnl"] if currency else (None if active else 0.0),
        "total_fees": by_currency[currency]["total_fees"] if currency else (None if active else 0.0),
        "by_currency": by_currency,
        "unclassified_runs": unclassified,
    }


async def _run_report(session: AsyncSession, run: RunRow, start: datetime, end: datetime) -> dict[str, Any]:
    snapshots = (
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(
                    EquitySnapshotRow.run_id == run.id,
                    EquitySnapshotRow.ts >= start,
                    EquitySnapshotRow.ts < end,
                )
                .order_by(EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )
    fills = (
        (
            await session.execute(
                select(FillRow)
                .where(FillRow.run_id == run.id, FillRow.ts >= start, FillRow.ts < end)
                .order_by(FillRow.ts)
            )
        )
        .scalars()
        .all()
    )
    # previous snapshot = day-start equity baseline
    prev = (
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == run.id, EquitySnapshotRow.ts < start)
                .order_by(EquitySnapshotRow.ts.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if not snapshots and not fills:
        # no activity today: inactive even if the run row looks alive
        # (e.g. a container-killed run that never got ended_at)
        return {
            "run_id": run.id,
            "mode": run.mode,
            "strategy_id": run.strategy_id,
            "status": run.status,
            "quote_currency": _quote_currency(run),
            "active": False,
        }

    # baseline: last snapshot before the day; falling back to starting cash
    # on the run's first day (the first in-day snapshot is already post-trade)
    start_equity = prev.equity if prev else (snapshots[0].equity if snapshots else None)
    if prev is None and isinstance((run.config or {}).get("starting_cash"), int | float):
        start_equity = run.config["starting_cash"]
    end_equity = snapshots[-1].equity if snapshots else start_equity
    pnl = (end_equity - start_equity) if start_equity is not None and end_equity is not None else None

    def to_domain(f: FillRow) -> Fill:
        return Fill(
            order_id=OrderId(f.order_id),
            pair=Pair.parse(f.pair),
            side=Side(f.side),
            ts=f.ts,
            price=f.price,
            size=f.size,
            fee=f.fee,
        )

    # seed with the position carried into the day so overnight round trips count
    pre_fills = (
        (
            await session.execute(
                select(FillRow).where(FillRow.run_id == run.id, FillRow.ts < start).order_by(FillRow.ts)
            )
        )
        .scalars()
        .all()
    )
    initial = open_position([to_domain(f) for f in pre_fills]) if pre_fills else (0.0, 0.0)
    trips = round_trips([to_domain(f) for f in fills], initial=initial)

    quote_currency = _quote_currency(run)
    fee_amounts: dict[str, Decimal] = {}
    unclassified_fee_fills = 0
    for fill in fills:
        # Legacy and paper fills omit the label: their fee uses the pair's
        # quote. Preserve an explicit exchange fee currency without FX.
        currency = (fill.fee_currency or Pair.parse(fill.pair).quote).upper()
        if currency == "MIXED":
            unclassified_fee_fills += 1
            continue
        fee_amounts[currency] = fee_amounts.get(currency, Decimal(0)) + Decimal(str(fill.fee))
    fees_by_currency = {currency: float(amount) for currency, amount in sorted(fee_amounts.items())}
    fees_paid = (
        fees_by_currency.get(quote_currency, 0.0)
        if quote_currency is not None
        and set(fees_by_currency) <= {quote_currency}
        and not unclassified_fee_fills
        else None
    )

    return {
        "run_id": run.id,
        "mode": run.mode,
        "strategy_id": run.strategy_id,
        "status": run.status,
        "quote_currency": quote_currency,
        "active": True,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "pnl": pnl,
        "num_fills": len(fills),
        "fees_paid": fees_paid,
        "fees_by_currency": fees_by_currency,
        "unclassified_fee_fills": unclassified_fee_fills,
        "round_trips": len(trips),
        "winning_trips": sum(1 for t in trips if t.pnl > 0),
        "max_equity": max((s.equity for s in snapshots), default=end_equity),
        "min_equity": min((s.equity for s in snapshots), default=end_equity),
    }


async def build_daily_report(
    sessionmaker: async_sessionmaker[AsyncSession], day: date, persist: bool = True
) -> dict[str, Any]:
    """Aggregate shadow/live runs active during ``day`` (UTC).

    Backtests are excluded: their equity timestamps are simulated, not real.
    Idempotently stored (one row per period, regenerated on demand)."""
    start, end = _day_bounds(day)
    async with sm_scope(sessionmaker) as session:
        runs = (
            (
                await session.execute(
                    select(RunRow)
                    .where(
                        RunRow.started_at < end,
                        RunRow.mode.in_(("shadow", "live")),
                        (RunRow.ended_at.is_(None)) | (RunRow.ended_at >= start),
                    )
                    .order_by(RunRow.started_at)
                )
            )
            .scalars()
            .all()
        )
        run_reports = [await _run_report(session, run, start, end) for run in runs]

    totals = _totals(run_reports)
    body: dict[str, Any] = {
        "period": day.isoformat(),
        "generated_at": utc_now().isoformat(),
        "runs": run_reports,
        "totals": totals,
    }

    if persist:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with sm_scope(sessionmaker) as session:
            stmt = pg_insert(ReportRow).values(
                id=new_id(), ts=utc_now(), period=day.isoformat(), run_id=None, body=body
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["period"],
                set_={"ts": stmt.excluded.ts, "body": stmt.excluded.body},
            )
            await session.execute(stmt)
    return body
