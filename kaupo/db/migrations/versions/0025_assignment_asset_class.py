"""Explicit asset class for supervised market-data routing."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_assignments", sa.Column("asset_class", sa.String(20), nullable=False, server_default="currency")
    )


def downgrade() -> None:
    op.drop_column("run_assignments", "asset_class")
