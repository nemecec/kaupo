"""Instance settings: read the effective shadow config, switch it at runtime."""

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import SettingsIn, SettingsOut
from kaupo.config import Settings, get_settings
from kaupo.data import assignments as assignments_repo
from kaupo.data import settings as settings_repo
from kaupo.db.models import EventRow, RunRow, SettingRow
from kaupo.db.session import get_session
from kaupo.domain import Pair, RunMode, RunStatus, Timeframe, new_id, utc_now
from kaupo.sdk.loader import load_strategies

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _settings_out(rows: Sequence[SettingRow]) -> SettingsOut:
    """Effective settings: stored values with built-in defaults filled in."""
    stored = {row.key: row.value for row in rows}
    updated = {row.key: row.updated_at for row in rows if row.key in settings_repo.SHADOW_DEFAULTS}
    effective: dict[str, Any] = {
        key: stored.get(key, default) for key, default in settings_repo.SHADOW_DEFAULTS.items()
    }
    return SettingsOut(**effective, updated_at=updated)


def _validate(body: SettingsIn, settings: Settings) -> dict[str, str]:
    """The given keys validated and normalized (pair uppercased, timeframe canonical)."""
    changes: dict[str, str] = {}
    if body.shadow_strategy is not None:
        strategies = load_strategies(settings.strategies_dir)
        if body.shadow_strategy not in strategies:
            raise HTTPException(
                status_code=422,
                detail=f"unknown strategy {body.shadow_strategy!r}; available: {sorted(strategies)}",
            )
        changes[settings_repo.SHADOW_STRATEGY_KEY] = body.shadow_strategy
    if body.shadow_pair is not None:
        try:
            changes[settings_repo.SHADOW_PAIR_KEY] = str(Pair.parse(body.shadow_pair))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.shadow_timeframe is not None:
        try:
            changes[settings_repo.SHADOW_TIMEFRAME_KEY] = Timeframe.parse(body.shadow_timeframe).value
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return changes


async def _notify_shadow_runs(
    session: AsyncSession, changed: dict[str, str], current: dict[str, Any]
) -> None:
    """Write a 'switch' control event for each shadow run matching the CURRENT settings.

    Only runs whose strategy and pair equal the pre-change settings are
    targeted: static side runs (--no-config-from-db) on other pairs keep
    running. The engine treats 'switch' as a graceful halt; the container
    restart policy then starts a new process, which reads the new settings.
    Backtest (and future live) runs are never targeted.
    """
    strategy = str(
        current.get(
            settings_repo.SHADOW_STRATEGY_KEY,
            settings_repo.SHADOW_DEFAULTS[settings_repo.SHADOW_STRATEGY_KEY],
        )
    )
    pair = str(
        current.get(
            settings_repo.SHADOW_PAIR_KEY,
            settings_repo.SHADOW_DEFAULTS[settings_repo.SHADOW_PAIR_KEY],
        )
    )
    runs = (
        (
            await session.execute(
                select(RunRow).where(
                    RunRow.mode == RunMode.SHADOW.value,
                    RunRow.status == RunStatus.RUNNING.value,
                    RunRow.strategy_id == strategy,
                    RunRow.config["pair"].as_string() == pair,
                )
            )
        )
        .scalars()
        .all()
    )
    ts = utc_now()
    for run in runs:
        session.add(
            EventRow(
                id=new_id(),
                ts=ts,
                level="info",
                source="control",
                message=f"control command 'switch' issued for run {run.id}",
                data={"command": "switch", "run_id": run.id, "settings": changed},
            )
        )


async def _sync_primary_assignment(session: AsyncSession) -> None:
    """Upsert the 'primary' run assignment from the effective settings.

    Facade for the agent prompt that only knows PUT /settings: the desired-
    state portfolio stays the source of truth, and the supervisor restarts
    the run from the updated row. Uses the effective values (stored keys
    over the built-in defaults), so all three fields are always written.
    """
    stored = await settings_repo.get_settings(session)
    effective = {key: str(stored.get(key, default)) for key, default in settings_repo.SHADOW_DEFAULTS.items()}
    strategy_id = effective[settings_repo.SHADOW_STRATEGY_KEY]
    pair = effective[settings_repo.SHADOW_PAIR_KEY]
    timeframe = effective[settings_repo.SHADOW_TIMEFRAME_KEY]
    primary = await assignments_repo.get_assignment(session, assignments_repo.PRIMARY_ASSIGNMENT_ID)
    if primary is None:
        await assignments_repo.create_assignment(
            session,
            id=assignments_repo.PRIMARY_ASSIGNMENT_ID,
            strategy_id=strategy_id,
            pair=pair,
            timeframe=timeframe,
            mode=RunMode.SHADOW.value,
        )
    else:
        await assignments_repo.update_assignment(
            session,
            assignments_repo.PRIMARY_ASSIGNMENT_ID,
            strategy_id=strategy_id,
            pair=pair,
            timeframe=timeframe,
        )


@router.get("")
async def read_settings(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettingsOut:
    rows = (await session.execute(select(SettingRow))).scalars().all()
    return _settings_out(rows)


@router.put("")
async def update_settings(
    body: SettingsIn,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SettingsOut:
    changes = _validate(body, settings)
    if not changes:
        raise HTTPException(status_code=422, detail="at least one shadow_* field is required")
    current = await settings_repo.get_settings(session)
    changed = {key: value for key, value in changes.items() if current.get(key) != value}
    if changed:
        await settings_repo.upsert_settings(session, changed)
        await _notify_shadow_runs(session, changed, current)
        await _sync_primary_assignment(session)
        from kaupo.core.notify import send_alert

        await send_alert(f"Shadow strategy switch requested: {changed}")
    rows = (await session.execute(select(SettingRow))).scalars().all()
    return _settings_out(rows)
