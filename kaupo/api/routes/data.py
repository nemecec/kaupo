"""Daily reports, candles, control commands, events."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import CandleOut, ControlIn, ControlOut, EventOut, ReportOut
from kaupo.data.candles import get_candles
from kaupo.db.models import EventRow
from kaupo.db.session import get_session, get_sessionmaker
from kaupo.domain import Pair, Timeframe, new_id, utc_now
from kaupo.report.daily import build_daily_report

router = APIRouter(prefix="/api/v1", tags=["data"])


@router.get("/reports/daily")
async def daily_report(
    _: Annotated[Principal, Depends(get_principal)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    day: date | None = Query(None),
) -> ReportOut:
    target = day or utc_now().date()
    body = await build_daily_report(sessionmaker, target)
    return ReportOut(
        period=body["period"],
        generated_at=body["generated_at"],
        runs=body["runs"],
        totals=body["totals"],
    )


@router.get("/candles")
async def candles(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pair: str = Query(...),
    timeframe: str = Query("1h"),
    start: str = Query(...),
    end: str = Query(...),
    limit: int = Query(5000, le=50_000),
) -> list[CandleOut]:
    from datetime import datetime

    rows = await get_candles(
        session,
        Pair.parse(pair),
        Timeframe.parse(timeframe),
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )
    return [
        CandleOut(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
        for c in rows[:limit]
    ]


@router.post("/control/{command}")
async def control(
    command: str,
    body: ControlIn,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ControlOut:
    if command not in ("pause", "resume", "kill"):
        raise HTTPException(status_code=400, detail="command must be pause|resume|kill")
    ts = utc_now()
    session.add(
        EventRow(
            id=new_id(),
            ts=ts,
            level="info",
            source="control",
            message=f"control command {command!r} issued for run {body.run_id or 'ALL'}",
            data={"command": command, "run_id": body.run_id},
        )
    )
    return ControlOut(command=command, run_id=body.run_id, issued_at=ts)


@router.get("/events")
async def events(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(100, le=1000),
    level: str | None = Query(None),
) -> list[EventOut]:
    stmt = select(EventRow).order_by(EventRow.ts.desc()).limit(limit)
    if level:
        stmt = stmt.where(EventRow.level == level)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        EventOut(id=r.id, ts=r.ts, level=r.level, source=r.source, message=r.message, data=r.data)
        for r in rows
    ]
