"""backtest_jobs: the durable queue for the backtest worker

One row per submitted backtest: the validated request body (``payload``),
a status (queued/running/completed/failed), and the resulting runs row id
or the failure reason. The API inserts; the worker claims and finishes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Index("ix_backtest_jobs_status_created", "status", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("backtest_jobs")
