"""Optional per-assignment fee schedules; null retains the global defaults."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_assignments", sa.Column("taker_fee_bps", sa.Float(), nullable=True))
    op.add_column("run_assignments", sa.Column("maker_fee_bps", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_assignments", "maker_fee_bps")
    op.drop_column("run_assignments", "taker_fee_bps")
