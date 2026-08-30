"""futures_metrics_daily: ratio columns become nullable

The Binance metrics archive leaves the four long/short ratio columns empty
for roughly a year (2021-12-22 to late 2022) while open interest stays
valid. Ratios are optional payload: null when the source has no values for
the day. Open interest stays required.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RATIO_COLUMNS = (
    "count_toptrader_ls_ratio",
    "sum_toptrader_ls_ratio",
    "count_ls_ratio",
    "taker_ls_vol_ratio",
)


def upgrade() -> None:
    for column in _RATIO_COLUMNS:
        op.alter_column("futures_metrics_daily", column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    for column in _RATIO_COLUMNS:
        op.alter_column("futures_metrics_daily", column, existing_type=sa.Float(), nullable=False)
