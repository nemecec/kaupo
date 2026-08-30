"""Run assignments: CRUD for the desired-state portfolio of trading runs."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import AssignmentIn, AssignmentOut, AssignmentUpdate
from kaupo.config import Settings, get_settings
from kaupo.data import assignments as assignments_repo
from kaupo.data.assignments import Assignment
from kaupo.db.models import RunRow
from kaupo.db.session import get_session
from kaupo.domain import Pair, RunMode, RunStatus, Timeframe, new_id
from kaupo.sdk.loader import load_strategies

router = APIRouter(prefix="/api/v1/assignments", tags=["assignments"])


async def _live_runs(session: AsyncSession) -> dict[tuple[str, str, str, str], str]:
    """Running runs keyed by (mode, strategy, config pair, config timeframe) → run id."""
    rows = (
        (await session.execute(select(RunRow).where(RunRow.status == RunStatus.RUNNING.value)))
        .scalars()
        .all()
    )
    return {
        (
            row.mode,
            row.strategy_id or "",
            str((row.config or {}).get("pair", "")),
            str((row.config or {}).get("timeframe", "")),
        ): row.id
        for row in rows
    }


def _assignment_out(assignment: Assignment, live: dict[tuple[str, str, str, str], str]) -> AssignmentOut:
    return AssignmentOut(
        id=assignment.id,
        strategy_id=assignment.strategy_id,
        pair=assignment.pair,
        pairs=assignment.pairs,
        timeframe=assignment.timeframe,
        mode=assignment.mode,
        params=assignment.params,
        enabled=assignment.enabled,
        starting_cash=assignment.starting_cash,
        created_at=assignment.created_at,
        updated_at=assignment.updated_at,
        run_id=live.get(
            (assignment.mode, assignment.strategy_id, assignment.pair, assignment.timeframe)
        ),
    )


def _validate_strategy(strategy_id: str, settings: Settings) -> None:
    strategies = load_strategies(settings.strategies_dir)
    if strategy_id not in strategies:
        raise HTTPException(
            status_code=422,
            detail=f"unknown strategy {strategy_id!r}; available: {sorted(strategies)}",
        )


def _validate_strategy_kind(strategy_id: str, portfolio: bool, settings: Settings) -> None:
    """The strategy kind must match the assignment: pairs need a portfolio strategy."""
    strategies = load_strategies(settings.strategies_dir)
    loaded = strategies.get(strategy_id)
    if loaded is None:
        return  # unknown ids are rejected elsewhere; nothing to check against
    if portfolio and not loaded.is_portfolio:
        raise HTTPException(status_code=422, detail=f"strategy {strategy_id!r} is not a portfolio strategy")
    if not portfolio and loaded.is_portfolio:
        raise HTTPException(
            status_code=422, detail=f"strategy {strategy_id!r} is a portfolio strategy; pass pairs"
        )


def _validate_params(strategy_id: str, params: dict[str, Any], settings: Settings) -> None:
    strategies = load_strategies(settings.strategies_dir)
    if strategy_id not in strategies:
        return  # unknown ids are rejected elsewhere; nothing to validate against
    try:
        strategies[strategy_id].create(params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_pair(pair: str) -> str:
    try:
        return str(Pair.parse(pair))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_timeframe(timeframe: str) -> str:
    try:
        return Timeframe.parse(timeframe).value
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_pairs(pairs: list[str]) -> list[str]:
    try:
        return assignments_repo.normalize_universe(pairs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_mode(mode: str) -> str:
    try:
        return RunMode(mode).value
    except ValueError:
        valid = ", ".join(m.value for m in RunMode)
        raise HTTPException(status_code=422, detail=f"unknown mode {mode!r}; valid: {valid}") from None


@router.get("")
async def list_assignments(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AssignmentOut]:
    rows = await assignments_repo.list_assignments(session)
    live = await _live_runs(session)
    return [_assignment_out(a, live) for a in rows]


@router.post("", status_code=201)
async def create_assignment(
    body: AssignmentIn,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssignmentOut:
    _validate_strategy(body.strategy_id, settings)
    _validate_strategy_kind(body.strategy_id, portfolio=body.pairs is not None, settings=settings)
    _validate_params(body.strategy_id, body.params, settings)
    assignment_id = body.id or new_id()
    if await assignments_repo.get_assignment(session, assignment_id) is not None:
        raise HTTPException(status_code=409, detail=f"assignment {assignment_id!r} already exists")
    pair = _validate_pair(body.pair) if body.pair is not None else ""
    pairs = _validate_pairs(body.pairs) if body.pairs is not None else None
    assignment = await assignments_repo.create_assignment(
        session,
        id=assignment_id,
        strategy_id=body.strategy_id,
        pair=pair,  # the repo derives the joined universe when pairs is set
        timeframe=_validate_timeframe(body.timeframe),
        mode=_validate_mode(body.mode),
        params=body.params,
        enabled=body.enabled,
        starting_cash=body.starting_cash,
        pairs=pairs,
    )
    return _assignment_out(assignment, await _live_runs(session))


@router.put("/{assignment_id}")
async def update_assignment(
    assignment_id: str,
    body: AssignmentUpdate,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssignmentOut:
    current = await assignments_repo.get_assignment(session, assignment_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"assignment {assignment_id!r} not found")
    changes: dict[str, Any] = {}
    if body.strategy_id is not None:
        _validate_strategy(body.strategy_id, settings)
        changes["strategy_id"] = body.strategy_id
    if body.pairs is not None:
        changes["pairs"] = _validate_pairs(body.pairs)  # the repo rewrites pair to the joined universe
    elif body.pair is not None:
        changes["pair"] = _validate_pair(body.pair)
        changes["pairs"] = None  # explicit: a pair update switches back to single-pair
    if body.timeframe is not None:
        changes["timeframe"] = _validate_timeframe(body.timeframe)
    if body.params is not None:
        changes["params"] = body.params
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.starting_cash is not None:
        changes["starting_cash"] = body.starting_cash
    if not changes:
        raise HTTPException(status_code=422, detail="at least one field is required")
    if "strategy_id" in changes or "pairs" in changes or "pair" in changes:
        strategy_id = str(changes.get("strategy_id", current.strategy_id))
        _validate_strategy_kind(
            strategy_id,
            portfolio=changes.get("pairs", current.pairs) is not None,
            settings=settings,
        )
    if "strategy_id" in changes or "params" in changes:
        _validate_params(
            str(changes.get("strategy_id", current.strategy_id)),
            changes.get("params", current.params),
            settings,
        )
    assignment = await assignments_repo.update_assignment(session, assignment_id, **changes)
    assert assignment is not None  # existence checked above
    return _assignment_out(assignment, await _live_runs(session))


@router.delete("/{assignment_id}")
async def delete_assignment(
    assignment_id: str,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentOut:
    """Soft delete: disables the row; the supervisor stops the run gracefully."""
    assignment = await assignments_repo.delete_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail=f"assignment {assignment_id!r} not found")
    return _assignment_out(assignment, await _live_runs(session))
