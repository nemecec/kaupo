"""Persist risk checkpoints separately from run configuration and metrics."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("risk_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "risk_state")
