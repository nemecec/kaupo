"""book_snapshots table: top-of-book observations (best bid/ask with sizes)

Forward collection only: no public API serves historical books. The primary
key is the observation (exchange, pair, ts): two polls that see the same
ticker timestamp collapse to one row, the natural dedupe for polling. The
table is bounded by retention pruning after each collector cycle.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "book_snapshots",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("pair", sa.String(20), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("bid", sa.Float, nullable=False),
        sa.Column("ask", sa.Float, nullable=False),
        sa.Column("bid_size", sa.Float, nullable=False),
        sa.Column("ask_size", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("book_snapshots")
