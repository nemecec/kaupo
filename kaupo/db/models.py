"""SQLAlchemy models. Mirror of the schema in the initial Alembic migration."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CandleRow(Base):
    __tablename__ = "candles"

    pair: Mapped[str] = mapped_column(String(20), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume: Mapped[float]


class StrategyRow(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    strategy_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_runs_mode_started", "mode", "started_at"),)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pair: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(6))
    size: Mapped[float]
    limit_price: Mapped[float | None] = mapped_column(nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(nullable=True)
    take_profit: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(9))
    filled_price: Mapped[float | None] = mapped_column(nullable=True)
    filled_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fee: Mapped[float] = mapped_column(default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pair: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float]
    size: Mapped[float]
    fee: Mapped[float]


class LedgerEntryRow(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    asset: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    reason: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    equity: Mapped[float]
    cash: Mapped[float]
    unrealized_pnl: Mapped[float]

    __table_args__ = (Index("ix_equity_run_ts", "run_id", "ts"),)


class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period: Mapped[str] = mapped_column(String(10), unique=True)  # e.g. "2026-08-23"
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_events_ts", "ts"),)
