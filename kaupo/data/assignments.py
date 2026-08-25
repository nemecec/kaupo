"""Run assignments: the desired set of trading runs, the single source of truth.

Each row declares one run: strategy, pair, timeframe, mode, params. The
supervisor reconciles live runs to the enabled rows; the API manages the
rows. ``updated_at`` bumps on every change — the supervisor treats an update
as a resume signal for a control-killed run.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import RunAssignmentRow
from kaupo.domain import RunMode, utc_now

# the settings facade (PUT /api/v1/settings) keeps this row in sync
PRIMARY_ASSIGNMENT_ID = "primary"


@dataclass(frozen=True)
class Assignment:
    id: str
    strategy_id: str
    pair: str
    timeframe: str
    mode: str
    params: dict[str, Any]
    enabled: bool
    starting_cash: float | None
    created_at: datetime
    updated_at: datetime


def _to_assignment(row: RunAssignmentRow) -> Assignment:
    return Assignment(
        id=row.id,
        strategy_id=row.strategy_id,
        pair=row.pair,
        timeframe=row.timeframe,
        mode=row.mode,
        params=row.params or {},
        enabled=row.enabled,
        starting_cash=row.starting_cash,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_assignments(session: AsyncSession, *, enabled_only: bool = False) -> list[Assignment]:
    """All rows, oldest first; only the enabled ones when ``enabled_only``."""
    stmt = select(RunAssignmentRow).order_by(RunAssignmentRow.created_at, RunAssignmentRow.id)
    if enabled_only:
        stmt = stmt.where(RunAssignmentRow.enabled.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_assignment(row) for row in rows]


async def get_assignment(session: AsyncSession, assignment_id: str) -> Assignment | None:
    row = await session.get(RunAssignmentRow, assignment_id)
    return _to_assignment(row) if row is not None else None


async def create_assignment(
    session: AsyncSession,
    *,
    id: str,
    strategy_id: str,
    pair: str,
    timeframe: str,
    mode: str = RunMode.SHADOW.value,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
    starting_cash: float | None = None,
) -> Assignment:
    now = utc_now()
    row = RunAssignmentRow(
        id=id,
        strategy_id=strategy_id,
        pair=pair,
        timeframe=timeframe,
        mode=mode,
        params=params or {},
        enabled=enabled,
        starting_cash=starting_cash,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return _to_assignment(row)


async def update_assignment(session: AsyncSession, assignment_id: str, **changes: Any) -> Assignment | None:
    """Apply the given field changes and bump updated_at; None when absent.

    Only keys matching row columns are applied; callers validate first.
    """
    row = await session.get(RunAssignmentRow, assignment_id)
    if row is None:
        return None
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = utc_now()
    await session.flush()
    return _to_assignment(row)


async def delete_assignment(session: AsyncSession, assignment_id: str) -> Assignment | None:
    """Soft delete: disables the row (kept for history); None when absent."""
    return await update_assignment(session, assignment_id, enabled=False)
