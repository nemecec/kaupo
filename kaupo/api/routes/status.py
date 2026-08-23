"""Health and system status."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal
from kaupo.api.schemas import StatusOut
from kaupo.db.models import CandleRow, RunRow
from kaupo.db.session import get_session

router = APIRouter(tags=["status"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/status")
async def system_status(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StatusOut:
    mode_rows = (
        await session.execute(
            select(RunRow.mode, func.count()).where(RunRow.status == "running").group_by(RunRow.mode)
        )
    ).all()
    runs_by_mode = {mode: count for mode, count in mode_rows}

    candle_rows = (
        await session.execute(
            select(CandleRow.pair, CandleRow.timeframe, func.count(), func.max(CandleRow.ts)).group_by(
                CandleRow.pair, CandleRow.timeframe
            )
        )
    ).all()
    candles = {
        f"{pair}/{tf}": {"count": count, "latest": latest.isoformat() if latest else None}
        for pair, tf, count, latest in candle_rows
    }

    return StatusOut(
        active_runs=sum(runs_by_mode.values()),
        runs_by_mode=runs_by_mode,
        candles=candles,
    )
