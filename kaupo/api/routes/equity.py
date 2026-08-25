"""Account-level equity: one curve stitched across sequential runs of a strategy."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal
from kaupo.api.schemas import EquityPoint
from kaupo.db.models import RunRow
from kaupo.db.session import get_session
from kaupo.report.equity import stitch_equity

router = APIRouter(prefix="/api/v1/equity", tags=["equity"])


@router.get("/account")
async def account_equity(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    strategy: str = Query(...),
    mode: str = Query("shadow"),
) -> list[EquityPoint]:
    # known strategies come from the runs table, not the loader: stitched
    # history must also work for strategies that are no longer deployed
    known = (await session.execute(select(RunRow.id).where(RunRow.strategy_id == strategy).limit(1))).first()
    if known is None:
        raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")
    points = await stitch_equity(session, mode, strategy)
    return [
        EquityPoint(ts=p.ts, equity=p.equity, cash=p.cash, unrealized_pnl=p.unrealized_pnl) for p in points
    ]
