"""Runs: list, detail, equity curve, orders, fills, positions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal
from kaupo.api.schemas import EquityPoint, FillOut, OrderOut, PositionOut, RunOut
from kaupo.db.models import CandleRow, EquitySnapshotRow, FillRow, OrderRow, RunRow
from kaupo.db.session import get_session

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def _run_out(row: RunRow) -> RunOut:
    return RunOut(
        id=row.id,
        mode=row.mode,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        started_at=row.started_at,
        ended_at=row.ended_at,
        status=row.status,
        config=row.config,
        metrics=row.metrics,
    )


@router.get("")
async def list_runs(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    mode: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[RunOut]:
    stmt = select(RunRow).order_by(RunRow.started_at.desc()).limit(limit).offset(offset)
    if mode:
        stmt = stmt.where(RunRow.mode == mode)
    if status:
        stmt = stmt.where(RunRow.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_run_out(r) for r in rows]


async def _get_run(session: AsyncSession, run_id: str) -> RunRow:
    row = await session.get(RunRow, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return row


@router.get("/{run_id}")
async def get_run(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: str,
) -> RunOut:
    return _run_out(await _get_run(session, run_id))


@router.get("/{run_id}/equity")
async def run_equity(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: str,
    limit: int = Query(5000, ge=1, le=50_000),
) -> list[EquityPoint]:
    await _get_run(session, run_id)
    rows = list(
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == run_id)
                .order_by(EquitySnapshotRow.ts.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()  # latest N, ascending for charting
    return [EquityPoint(ts=r.ts, equity=r.equity, cash=r.cash, unrealized_pnl=r.unrealized_pnl) for r in rows]


@router.get("/{run_id}/orders")
async def run_orders(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: str,
    limit: int = Query(1000, ge=1, le=10_000),
) -> list[OrderOut]:
    await _get_run(session, run_id)
    rows = list(
        (
            await session.execute(
                select(OrderRow).where(OrderRow.run_id == run_id).order_by(OrderRow.ts.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()  # latest N, ascending
    return [
        OrderOut(
            id=r.id,
            ts=r.ts,
            pair=r.pair,
            side=r.side,
            type=r.type,
            size=r.size,
            limit_price=r.limit_price,
            status=r.status,
            filled_price=r.filled_price,
            filled_ts=r.filled_ts,
            fee=r.fee,
            reason=r.reason,
        )
        for r in rows
    ]


@router.get("/{run_id}/trades")
async def run_trades(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: str,
    limit: int = Query(1000, ge=1, le=10_000),
) -> list[FillOut]:
    await _get_run(session, run_id)
    rows = list(
        (
            await session.execute(
                select(FillRow).where(FillRow.run_id == run_id).order_by(FillRow.ts.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()  # latest N, ascending
    return [
        FillOut(
            id=r.id,
            order_id=r.order_id,
            ts=r.ts,
            pair=r.pair,
            side=r.side,
            price=r.price,
            size=r.size,
            fee=r.fee,
        )
        for r in rows
    ]


@router.get("/{run_id}/positions")
async def run_positions(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: str,
) -> list[PositionOut]:
    run = await _get_run(session, run_id)
    fills = (
        (await session.execute(select(FillRow).where(FillRow.run_id == run_id).order_by(FillRow.ts)))
        .scalars()
        .all()
    )

    # net position per pair with FIFO average cost
    positions: dict[str, tuple[float, float]] = {}  # pair -> (qty, total_cost)
    for f in fills:
        qty, cost = positions.get(f.pair, (0.0, 0.0))
        if f.side == "buy":
            positions[f.pair] = (qty + f.size, cost + f.price * f.size + f.fee)
        elif qty > 0:
            avg = cost / qty
            closed = min(f.size, qty)
            positions[f.pair] = (qty - closed, cost - closed * avg)

    timeframe = (run.config or {}).get("timeframe", "1h")
    # mark at the run's own timeline (equity-snapshot based: correct for
    # backtests whose candles are simulated-time, unlike wall-clock ended_at)
    last_snap_ts = (
        (
            await session.execute(
                select(EquitySnapshotRow.ts)
                .where(EquitySnapshotRow.run_id == run_id)
                .order_by(EquitySnapshotRow.ts.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    price_cutoff = last_snap_ts or run.ended_at
    out = []
    for pair, (qty, cost) in positions.items():
        if qty <= 0:
            continue
        stmt = select(CandleRow.close).where(CandleRow.pair == pair, CandleRow.timeframe == timeframe)
        if price_cutoff is not None:
            stmt = stmt.where(CandleRow.ts <= price_cutoff)
        last_price = (await session.execute(stmt.order_by(CandleRow.ts.desc()).limit(1))).scalars().first()
        out.append(
            PositionOut(
                pair=pair,
                size=qty,
                avg_entry=cost / qty,
                last_price=last_price,
                market_value=qty * last_price if last_price else None,
            )
        )
    return out
