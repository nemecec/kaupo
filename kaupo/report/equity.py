"""Account-level equity: one curve stitched across the sequential runs of a strategy.

Every deploy/restart starts a new run with a fresh ledger, so a per-run equity
curve resets. Stitching rebases each run's snapshots onto the end of the
previous run, giving one continuous account-level series. The stored per-run
snapshots are unchanged — the rebase is a read-time view.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import EquitySnapshotRow, RunRow


@dataclass(frozen=True)
class StitchedPoint:
    ts: datetime
    equity: float
    cash: float
    unrealized_pnl: float


async def stitch_equity(session: AsyncSession, mode: str, strategy_id: str) -> list[StitchedPoint]:
    """Continuous equity curve for (mode, strategy_id) across runs, ascending by ts.

    Runs are chained oldest to newest: each run's snapshots are offset so the
    run's first stitched point equals the previous run's last stitched point.
    Runs without snapshots are skipped. Where run timelines overlap, the later
    run wins: earlier-run points at or after the next run's first snapshot are
    dropped. Equity and cash carry the offset, so equity - cash = unrealized
    P&L still holds for every stitched point.
    """
    runs = list(
        (
            await session.execute(
                select(RunRow)
                .where(RunRow.mode == mode, RunRow.strategy_id == strategy_id)
                .order_by(RunRow.started_at.asc(), RunRow.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return []
    snapshots = list(
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id.in_([r.id for r in runs]))
                .order_by(EquitySnapshotRow.run_id, EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )
    by_run: dict[str, list[EquitySnapshotRow]] = {}
    for s in snapshots:
        by_run.setdefault(s.run_id, []).append(s)

    stitched: list[StitchedPoint] = []
    for run in runs:
        rows = by_run.get(run.id)
        if not rows:
            continue
        first_ts = rows[0].ts
        while stitched and stitched[-1].ts >= first_ts:
            stitched.pop()  # overlap: the later run wins
        offset = stitched[-1].equity - rows[0].equity if stitched else 0.0
        stitched.extend(
            StitchedPoint(
                ts=r.ts,
                equity=r.equity + offset,
                cash=r.cash + offset,
                unrealized_pnl=r.unrealized_pnl,
            )
            for r in rows
        )
    return stitched
