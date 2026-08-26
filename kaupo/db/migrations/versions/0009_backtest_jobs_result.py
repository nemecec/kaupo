"""backtest_jobs.result: the stability-window aggregation of a completed job

When a backtest request asks for stability windows, the worker stores the
per-window aggregation ({"windows": K, "slices": [...]}) here; the API
returns it as ``stability`` on the completed job. Null for plain jobs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_jobs", sa.Column("result", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_jobs", "result")
