"""run_assignments table: the desired set of trading runs

Seeds the current state: the settings-driven shadow run as 'primary' (from
the settings table when present, else the built-in defaults) and the static
SOL side run as 'sol-4h'.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# mirrors SHADOW_DEFAULTS in kaupo/data/settings.py (kept literal: migrations
# must stay valid even when the application code changes)
_DEFAULTS = {"shadow_strategy": "regime-switch", "shadow_pair": "BTC/EUR", "shadow_timeframe": "1h"}


def _setting(connection: Any, key: str) -> str:
    """Stored settings value, or the built-in default when absent.

    The value column is JSON: depending on the driver it comes back decoded
    (``sma-cross``) or as raw JSON text (``"sma-cross"``); handle both.
    """
    value = connection.execute(sa.text("SELECT value FROM settings WHERE key = :key"), {"key": key}).scalar()
    if value is None:
        return _DEFAULTS[key]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed if isinstance(parsed, str) else str(parsed)
    return str(value)


def upgrade() -> None:
    op.create_table(
        "run_assignments",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("pair", sa.Text, nullable=False),
        sa.Column("timeframe", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("params", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("starting_cash", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    now = datetime.now(UTC)
    table = sa.table(
        "run_assignments",
        sa.column("id", sa.Text),
        sa.column("strategy_id", sa.Text),
        sa.column("pair", sa.Text),
        sa.column("timeframe", sa.Text),
        sa.column("mode", sa.Text),
        sa.column("params", sa.JSON),
        sa.column("enabled", sa.Boolean),
        sa.column("starting_cash", sa.Float),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": "primary",
                "strategy_id": _setting(connection, "shadow_strategy"),
                "pair": _setting(connection, "shadow_pair"),
                "timeframe": _setting(connection, "shadow_timeframe"),
                "mode": "shadow",
                "params": {},
                "enabled": True,
                "starting_cash": None,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "sol-4h",
                "strategy_id": "sma-cross",
                "pair": "SOL/EUR",
                "timeframe": "4h",
                "mode": "shadow",
                "params": {},
                "enabled": True,
                "starting_cash": None,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("run_assignments")
