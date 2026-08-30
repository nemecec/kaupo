"""open_interest table: forward-collected perpetual-futures positioning signal

Binance serves only ~30 days of open-interest history, so deep history
cannot be backfilled. This table accumulates hourly snapshots (one row per
exchange, base asset, hour) from the daily refresh cron and is never pruned.
Same advisory role as funding_rates: a positioning signal, not a traded
instrument.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "open_interest",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("base_asset", sa.String(20), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("oi_base", sa.Float, nullable=False),
        sa.Column("oi_quote", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("open_interest")
