"""Daily machine-readable performance reports — the agent feedback artifact."""

from datetime import UTC, date, datetime, timedelta
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
            "active": False,
        }

    start_equity = prev.equity if prev else (snapshots[0].equity if snapshots else None)
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

    return {
        "run_id": run.id,
        "mode": run.mode,
        "strategy_id": run.strategy_id,
        "status": run.status,
        "active": True,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "pnl": pnl,
        "num_fills": len(fills),
        "fees_paid": round(sum(f.fee for f in fills), 2),
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

    active = [r for r in run_reports if r.get("active")]
    totals = {
        "num_runs": len(run_reports),
        "active_runs": len(active),
        "total_pnl": round(sum(r["pnl"] for r in active if r.get("pnl") is not None), 2),
        "total_fills": sum(r.get("num_fills", 0) for r in active),
        "total_fees": round(sum(r.get("fees_paid", 0.0) for r in active), 2),
    }
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
