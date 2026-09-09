"""orders: carry the exchange order id of a live order

Live runs place real orders on Kraken, which answers with its own order id
(a txid). Crash reconciliation needs that identity in the audit trail: on
restart it reads the account's trades back from Kraken and has to tell a
trade the platform already recorded from one it missed. Without the txid on
the order row, the only link between a Kraken trade and a recorded fill is a
timestamp, and Kraken stamps the trades of one order with the same
millisecond.

Null for paper orders (backtest and shadow), which never reach an exchange.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("exchange_order_id", sa.String(64), nullable=True))
    op.create_index("ix_orders_exchange_order_id", "orders", ["exchange_order_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_exchange_order_id", table_name="orders")
    op.drop_column("orders", "exchange_order_id")
