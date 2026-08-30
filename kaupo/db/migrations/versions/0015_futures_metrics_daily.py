"""futures_metrics_daily table: deep futures positioning history per base asset

Backfilled from the Binance USD-M perp metrics archive (5-minute rows,
aggregated to one row per exchange, base asset, and UTC day) and topped up
by the daily refresh. Open interest is the end-of-day snapshot; long/short
ratios are day means. Advisory positioning signal, never a traded
instrument. Never pruned.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "futures_metrics_daily",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("base_asset", sa.String(20), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("oi_base", sa.Float, nullable=False),
        sa.Column("oi_quote", sa.Float, nullable=False),
        sa.Column("count_toptrader_ls_ratio", sa.Float, nullable=False),
        sa.Column("sum_toptrader_ls_ratio", sa.Float, nullable=False),
        sa.Column("count_ls_ratio", sa.Float, nullable=False),
        sa.Column("taker_ls_vol_ratio", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("futures_metrics_daily")
