"""candles exchange dimension

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candles",
        sa.Column("exchange", sa.String(20), nullable=False, server_default="kraken"),
    )
    op.drop_constraint("candles_pkey", "candles", type_="primary")
    op.create_primary_key("candles_pkey", "candles", ["exchange", "pair", "timeframe", "ts"])


def downgrade() -> None:
    op.drop_constraint("candles_pkey", "candles", type_="primary")
    op.create_primary_key("candles_pkey", "candles", ["pair", "timeframe", "ts"])
    op.drop_column("candles", "exchange")
