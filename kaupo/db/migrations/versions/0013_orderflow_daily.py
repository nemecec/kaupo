"""orderflow_daily table: permanent daily order-flow aggregates per pair

The raw order-flow stores (trade_ticks, book_snapshots) are retention-capped
at a rolling 30 days, so long-window order-flow history lives here: one row
per (exchange, pair, UTC day), rolled up daily from the raw stores. The
aggregates are never pruned. Spread fields are null on days without book
snapshots.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orderflow_daily",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("pair", sa.String(20), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("trade_count", sa.Integer, nullable=False),
        sa.Column("buy_count", sa.Integer, nullable=False),
        sa.Column("sell_count", sa.Integer, nullable=False),
        sa.Column("buy_volume", sa.Float, nullable=False),
        sa.Column("sell_volume", sa.Float, nullable=False),
        sa.Column("max_trade_size", sa.Float, nullable=False),
        sa.Column("book_snapshots", sa.Integer, nullable=False),
        sa.Column("spread_mean_bps", sa.Float, nullable=True),
        sa.Column("spread_max_bps", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("orderflow_daily")
