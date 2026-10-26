"""Preserve optional exchange trade IDs without collapsing identical prints."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

LEGACY_KEY = ["exchange", "pair", "ts", "price", "size", "side"]


def upgrade() -> None:
    op.add_column("trade_ticks", sa.Column("trade_id", sa.String(100), nullable=False, server_default=""))
    op.drop_constraint("trade_ticks_pkey", "trade_ticks", type_="primary")
    op.create_primary_key("trade_ticks_pkey", "trade_ticks", [*LEGACY_KEY, "trade_id"])
    op.create_index(
        "uq_trade_ticks_source_id",
        "trade_ticks",
        ["exchange", "pair", "trade_id"],
        unique=True,
        postgresql_where=sa.text("trade_id <> ''"),
    )


def downgrade() -> None:
    # Refuse a downgrade that would collapse distinct source trades.
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM trade_ticks GROUP BY exchange, pair, ts, price, size, side HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate:
        raise RuntimeError("Cannot remove trade IDs while distinct trades share a legacy key")
    op.drop_index("uq_trade_ticks_source_id", table_name="trade_ticks")
    op.drop_constraint("trade_ticks_pkey", "trade_ticks", type_="primary")
    op.create_primary_key("trade_ticks_pkey", "trade_ticks", LEGACY_KEY)
    op.drop_column("trade_ticks", "trade_id")
