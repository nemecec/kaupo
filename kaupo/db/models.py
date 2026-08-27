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

    exchange: Mapped[str] = mapped_column(String(20), primary_key=True, server_default="kraken")
    pair: Mapped[str] = mapped_column(String(20), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume: Mapped[float]


class FundingRateRow(Base):
    """Perpetual-futures funding rates, keyed by BASE ASSET (e.g. "BTC").

    Funding marks crowded positioning and is used as an advisory filter
    signal, so one dominant USDT-margined perpetual per venue is enough;
    per-pair granularity is not needed.
    """

    __tablename__ = "funding_rates"

    exchange: Mapped[str] = mapped_column(String(20), primary_key=True)
    base_asset: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    rate: Mapped[float]


class TradeTickRow(Base):
    """Public trade prints (order flow), keyed by the full tick tuple.

    Kraken serves no trade id for public trades, so identical trades at the
    same ms with the same price and size collapse to one row. This dedupe
    heuristic can drop true same-ms duplicates; that is accepted for
    order-flow analytics (not audit-grade). Bounded by retention pruning
    after each ingest run.
    """

    __tablename__ = "trade_ticks"

    exchange: Mapped[str] = mapped_column(String(20), primary_key=True)
    pair: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    price: Mapped[float] = mapped_column(primary_key=True)
    size: Mapped[float] = mapped_column(primary_key=True)
    side: Mapped[str] = mapped_column(String(4), primary_key=True)


class BookSnapshotRow(Base):
    """Top-of-book snapshots (best bid/ask with sizes), keyed by observation.

    Identity is (exchange, pair, ts): two polls that see the same ticker
    timestamp collapse to one row, the natural dedupe for forward
    collection. Bounded by retention pruning after each collector cycle.
    """

    __tablename__ = "book_snapshots"

    exchange: Mapped[str] = mapped_column(String(20), primary_key=True)
    pair: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    bid: Mapped[float]
    ask: Mapped[float]
    bid_size: Mapped[float]
    ask_size: Mapped[float]


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
    # daily rows key on the day ("2026-08-23"); typed reports key on
    # "<type>-<period>" ("rolling-origin-2026-W35") — one upsert per period
    period: Mapped[str] = mapped_column(String(40), unique=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON)


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunAssignmentRow(Base):
    __tablename__ = "run_assignments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(Text)
    pair: Mapped[str] = mapped_column(Text)
    # null = single-pair run; a list = portfolio universe (pair holds the
    # comma-joined sorted list then, same convention as the run config)
    pairs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    timeframe: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    starting_cash: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BacktestJobRow(Base):
    __tablename__ = "backtest_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10))  # queued/running/completed/failed
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)  # the validated BacktestIn body
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the runs row id
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # the stability-window aggregation ({"windows", "slices"}); null when not requested
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_backtest_jobs_status_created", "status", "created_at"),)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_events_ts", "ts"),)
