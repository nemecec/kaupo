"""Instance settings: read the effective shadow config, switch it at runtime."""

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin
from kaupo.api.schemas import SettingsIn, SettingsOut
from kaupo.config import Settings, get_settings
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


async def _notify_shadow_runs(session: AsyncSession, changed: dict[str, str]) -> None:
    """Write a 'switch' control event for each running shadow run.

    The engine treats 'switch' as a graceful halt; the container restart
    policy then starts a new process, which reads the new settings from the
    settings table. Backtest (and future live) runs are never targeted.
    """
    runs = (
        (
            await session.execute(
                select(RunRow).where(
                    RunRow.mode == RunMode.SHADOW.value,
                    RunRow.status == RunStatus.RUNNING.value,
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
        await _notify_shadow_runs(session, changed)
    rows = (await session.execute(select(SettingRow))).scalars().all()
    return _settings_out(rows)
