"""unique reports.period

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # dedupe defensively before adding the constraint
    op.execute(
        """
        DELETE FROM reports a USING reports b
        WHERE a.period = b.period AND a.id < b.id
        """
    )
    op.drop_index("ix_reports_period", table_name="reports")
    op.create_unique_constraint("uq_reports_period", "reports", ["period"])


def downgrade() -> None:
    op.drop_constraint("uq_reports_period", "reports", type_="unique")
    op.create_index("ix_reports_period", "reports", ["period"])
