"""reports period: widen for typed period keys

The rolling-origin report keys its weekly rows like
"rolling-origin-2026-W35" (22 chars); the daily report's "2026-08-23"
shape stays valid.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("reports", "period", existing_type=sa.String(10), type_=sa.String(40))


def downgrade() -> None:
    # fails while a typed period key longer than 10 chars exists
    op.alter_column("reports", "period", existing_type=sa.String(40), type_=sa.String(10))
