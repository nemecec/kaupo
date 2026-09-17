"""Bound research work admitted to the host shared with live trading."""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import BacktestJobRow, RunAssignmentRow

MAX_SHADOW_ASSIGNMENTS = 12
MAX_PENDING_BACKTESTS = 6


async def check_research_capacity(session: AsyncSession, *, backtest: bool) -> None:
    # One transaction lock serializes the count and insertion across API workers.
    # The caller retains the lock until the request transaction commits.
    await session.execute(select(func.pg_advisory_xact_lock(71420831)))
    if backtest:
        count = await session.scalar(
            select(func.count())
            .select_from(BacktestJobRow)
            .where(BacktestJobRow.status.in_(("queued", "running")))
        )
        ceiling = MAX_PENDING_BACKTESTS
        detail = "research backtest queue is full; wait for current jobs"
    else:
        count = await session.scalar(
            select(func.count())
            .select_from(RunAssignmentRow)
            .where(RunAssignmentRow.mode == "shadow", RunAssignmentRow.enabled.is_(True))
        )
        ceiling = MAX_SHADOW_ASSIGNMENTS
        detail = "research shadow capacity reached; disable an unused assignment first"
    if (count or 0) >= ceiling:
        raise HTTPException(429, detail)
