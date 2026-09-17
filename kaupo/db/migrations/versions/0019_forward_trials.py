"""Prospective trials and an append-only research cost ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forward_trials",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("assignment_id", sa.String(100), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("root_run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("frozen_config", sa.JSON(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("baseline_equity", sa.Float(), nullable=False),
    )
    op.create_index("ix_forward_trials_assignment_id", "forward_trials", ["assignment_id"])
    op.create_table(
        "research_ledger",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("reference", sa.String(200), nullable=False, unique=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("amount_eur", sa.Numeric(14, 2), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("research_ledger")
    op.drop_index("ix_forward_trials_assignment_id", table_name="forward_trials")
    op.drop_table("forward_trials")
