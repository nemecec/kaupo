"""Optional trading hours for the assignment watchdog; null means 24/7."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_assignments", sa.Column("trading_schedule", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_assignments", "trading_schedule")
