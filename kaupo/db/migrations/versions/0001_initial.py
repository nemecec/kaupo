"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("pair", sa.String(20), primary_key=True),
        sa.Column("timeframe", sa.String(4), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
    )
    op.create_table(
        "strategies",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=True),
        sa.Column("strategy_version", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("metrics", sa.JSON, nullable=True),
        sa.Index("ix_runs_mode_started", "mode", "started_at"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pair", sa.String(20), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("type", sa.String(6), nullable=False),
        sa.Column("size", sa.Float, nullable=False),
        sa.Column("limit_price", sa.Float, nullable=True),
        sa.Column("stop_loss", sa.Float, nullable=True),
        sa.Column("take_profit", sa.Float, nullable=True),
        sa.Column("status", sa.String(9), nullable=False),
        sa.Column("filled_price", sa.Float, nullable=True),
        sa.Column("filled_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fee", sa.Float, nullable=False, server_default="0"),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=False, index=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pair", sa.String(20), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("size", sa.Float, nullable=False),
        sa.Column("fee", sa.Float, nullable=False),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("balance_after", sa.Numeric(38, 18), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("ref_id", sa.String(32), nullable=True),
    )
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Float, nullable=False),
        sa.Column("cash", sa.Float, nullable=False),
        sa.Column("unrealized_pnl", sa.Float, nullable=False),
        sa.Index("ix_equity_run_ts", "run_id", "ts"),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period", sa.String(10), nullable=False, index=True),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("body", sa.JSON, nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=True),
        sa.Index("ix_events_ts", "ts"),
    )


def downgrade() -> None:
    for table in (
        "events",
        "reports",
        "equity_snapshots",
        "ledger_entries",
        "fills",
        "orders",
        "runs",
        "strategies",
        "candles",
    ):
        op.drop_table(table)
