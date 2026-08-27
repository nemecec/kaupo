"""trade_ticks table: public trade prints (order flow) per pair

Kraken serves no trade id for public trades, so the primary key is the full
tick tuple: identical trades at the same ms with the same price and size
collapse to one row. This dedupe heuristic can drop true same-ms duplicates;
that is accepted for order-flow analytics (not audit-grade). The table is
bounded by retention pruning after each ingest run.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_ticks",
        sa.Column("exchange", sa.String(20), primary_key=True),
        sa.Column("pair", sa.String(20), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("price", sa.Float, primary_key=True),
        sa.Column("size", sa.Float, primary_key=True),
        sa.Column("side", sa.String(4), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("trade_ticks")
