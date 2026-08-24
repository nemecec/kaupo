"""Instance settings: key-value rows seeded once, updated through the API.

Single-user instance settings. The shadow run reads its strategy, pair, and
timeframe from here at startup, so an operator can switch them through the
API without a redeploy. CLI flags only seed a fresh database
(insert-if-absent); a stored value always wins over flags and defaults.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import SettingRow
from kaupo.domain import utc_now

SHADOW_STRATEGY_KEY = "shadow_strategy"
SHADOW_PAIR_KEY = "shadow_pair"
SHADOW_TIMEFRAME_KEY = "shadow_timeframe"

# built-in fallbacks when neither a CLI flag nor a stored setting exists
SHADOW_DEFAULTS: dict[str, str] = {
    SHADOW_STRATEGY_KEY: "regime-switch",
    SHADOW_PAIR_KEY: "BTC/EUR",
    SHADOW_TIMEFRAME_KEY: "1h",
}


@dataclass(frozen=True)
class ShadowSettings:
    strategy: str
    pair: str
    timeframe: str


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    """All stored settings, key → value."""
    rows = (await session.execute(select(SettingRow))).scalars().all()
    return {row.key: row.value for row in rows}


async def upsert_settings(session: AsyncSession, values: dict[str, Any]) -> None:
    """Insert or overwrite the given keys; bumps updated_at.

    ORM-based (not a Core upsert) so rows already loaded in this session's
    identity map reflect the new values on the next select.
    """
    if not values:
        return
    now = utc_now()
    rows = await session.execute(select(SettingRow).where(SettingRow.key.in_(values)))
    existing = {row.key: row for row in rows.scalars()}
    for key, value in values.items():
        row = existing.get(key)
        if row is None:
            session.add(SettingRow(key=key, value=value, updated_at=now))
        else:
            row.value = value
            row.updated_at = now
    await session.flush()


async def get_or_seed(session: AsyncSession, key: str, default: str) -> str:
    """Stored value for ``key``; inserts ``default`` and returns it when ABSENT.

    Insert-if-absent: an existing key is never overwritten, so seeding a fresh
    database from CLI flags is safe but cannot clobber a later API change.
    """
    stmt = (
        insert(SettingRow)
        .values([{"key": key, "value": default, "updated_at": utc_now()}])
        .on_conflict_do_nothing(constraint="settings_pkey")
    )
    await session.execute(stmt)
    result = await session.execute(select(SettingRow.value).where(SettingRow.key == key))
    value = result.scalar_one()
    assert isinstance(value, str)  # settings values are strings
    return value


async def resolve_shadow_config(
    session: AsyncSession,
    strategy: str | None = None,
    pair: str | None = None,
    timeframe: str | None = None,
) -> ShadowSettings:
    """Effective shadow config for a run about to start.

    Per key, the CLI flag seeds the row when absent; the effective value is
    always the stored one. A fresh database picks up the CLI flags (or the
    built-in defaults), and a restart after an API change picks up the new
    stored values — the flags in the compose command never override them.
    """
    return ShadowSettings(
        strategy=await get_or_seed(
            session, SHADOW_STRATEGY_KEY, strategy or SHADOW_DEFAULTS[SHADOW_STRATEGY_KEY]
        ),
        pair=await get_or_seed(session, SHADOW_PAIR_KEY, pair or SHADOW_DEFAULTS[SHADOW_PAIR_KEY]),
        timeframe=await get_or_seed(
            session, SHADOW_TIMEFRAME_KEY, timeframe or SHADOW_DEFAULTS[SHADOW_TIMEFRAME_KEY]
        ),
    )
