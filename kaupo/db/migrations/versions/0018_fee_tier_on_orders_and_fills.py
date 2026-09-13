"""Record which fee tier a fill paid, and the currency the fee was charged in.

Kraken reports both per trade, and ccxt already parses the tier into
``takerOrMaker``. The platform dropped it, so the first live fill needed a
manual Kraken lookup to answer "was this the maker fee?" (kaupo#42). With
these columns the order and fill rows answer it on their own.

Null on every row written before this migration, and on paper rows whose
venue did not report a tier.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("orders", "fills"):
        op.add_column(table, sa.Column("taker_or_maker", sa.String(6), nullable=True))
        op.add_column(table, sa.Column("fee_currency", sa.String(10), nullable=True))


def downgrade() -> None:
    for table in ("orders", "fills"):
        op.drop_column(table, "fee_currency")
        op.drop_column(table, "taker_or_maker")
