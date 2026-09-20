"""Keep every declared research variant and its immutable data snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_experiments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("reference", sa.String(200), nullable=False, unique=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("economics", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=True),
        sa.Column("dataset", sa.LargeBinary(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("research_experiments")
