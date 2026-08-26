"""run_assignments.pairs: the universe of a portfolio assignment

Nullable JSON list of pair strings. Null means a single-pair assignment
(``pair`` is the run pair). A list means a portfolio assignment: ``pair``
then stores the comma-joined sorted universe, the same convention as the
run config.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_assignments", sa.Column("pairs", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("run_assignments", "pairs")
