"""Daily reports, candles, control commands, events."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import (
    BookSnapshotOut,
    CandleOut,
    ControlIn,
    ControlOut,
    EventOut,
    FundingOut,
    ReportOut,
    TradeTickOut,
)
from kaupo.data.book import get_book_snapshots
from kaupo.data.candles import get_candles
from kaupo.data.funding import get_funding_rates
from kaupo.data.trades import get_trade_ticks
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
    start: datetime = Query(...),
    end: datetime = Query(...),
    limit: int = Query(5000, ge=1, le=50_000),
    exchange: str = Query("kraken"),
) -> list[CandleOut]:
    try:
        parsed_pair = Pair.parse(pair)
        parsed_tf = Timeframe.parse(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await get_candles(
        session,
        parsed_pair,
        parsed_tf,
        _aware(start),
        _aware(end),
        limit=limit,
        exchange=exchange,
    )
    return [
        CandleOut(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume) for c in rows
    ]


@router.get("/funding")
async def funding(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pair: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    limit: int = Query(5000, ge=1, le=50_000),
    exchange: str = Query("binance"),
) -> list[FundingOut]:
    """Funding rates for the pair's base asset. The venue is Binance (the
    ingest source), not the trade venue — the series marks crowded
    positioning market-wide, so it transfers across venues."""
    try:
        parsed_pair = Pair.parse(pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await get_funding_rates(
        session,
        exchange,
        parsed_pair.base,
        _aware(start),
        _aware(end),
        limit=limit,
    )
    return [FundingOut(exchange=r.exchange, base_asset=r.base_asset, ts=r.ts, rate=r.rate) for r in rows]


@router.get("/trades")
async def trades(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pair: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    limit: int = Query(5000, ge=1, le=50_000),
    exchange: str = Query("kraken"),
) -> list[TradeTickOut]:
    """Public trade prints (order flow) for the pair, ascending. Bounded by
    the ingest retention window, so deep history may be absent."""
    try:
        parsed_pair = Pair.parse(pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await get_trade_ticks(
        session,
        exchange,
        str(parsed_pair),
        _aware(start),
        _aware(end),
        limit=limit,
    )
    return [
        TradeTickOut(exchange=t.exchange, pair=t.pair, ts=t.ts, price=t.price, size=t.size, side=t.side)
        for t in rows
    ]


@router.get("/book")
async def book(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    pair: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    limit: int = Query(5000, ge=1, le=50_000),
    exchange: str = Query("kraken"),
) -> list[BookSnapshotOut]:
    """Top-of-book snapshots (best bid/ask with sizes) for the pair,
    ascending. Forward-collected by the book collector and bounded by its
    retention window, so history starts when the collector started."""
    try:
        parsed_pair = Pair.parse(pair)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await get_book_snapshots(
        session,
        exchange,
        str(parsed_pair),
        _aware(start),
        _aware(end),
        limit=limit,
    )
    return [
        BookSnapshotOut(
            exchange=s.exchange,
            pair=s.pair,
            ts=s.ts,
            bid=s.bid,
            ask=s.ask,
            bid_size=s.bid_size,
            ask_size=s.ask_size,
        )
        for s in rows
    ]


def _aware(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC (never host-local)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@router.post("/control/{command}")
async def control(
    command: str,
    body: ControlIn,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ControlOut:
    if command not in ("pause", "resume", "kill"):
        raise HTTPException(status_code=400, detail="command must be pause|resume|kill")
    if body.run_id is not None:
        from kaupo.db.models import RunRow

        if await session.get(RunRow, body.run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {body.run_id} not found")
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
    if command == "kill":
        from kaupo.core.notify import send_alert

        await send_alert(f"Kill switch used: run {body.run_id or 'ALL'}")
    return ControlOut(command=command, run_id=body.run_id, issued_at=ts)


@router.get("/events")
async def events(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(100, ge=1, le=1000),
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
