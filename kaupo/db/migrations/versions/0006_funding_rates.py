"""funding_rates table: perpetual-futures funding history per base asset

Funding is keyed by base asset (e.g. "BTC"), not by pair: one dominant
USDT-margined perpetual per venue is enough for a positioning filter signal.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funding_rates",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("base_asset", sa.String(20), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("rate", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("funding_rates")
