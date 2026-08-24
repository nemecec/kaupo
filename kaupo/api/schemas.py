"""API request/response schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class StatusOut(BaseModel):
    status: Literal["ok"] = "ok"
    active_runs: int
    runs_by_mode: dict[str, int]
    candles: dict[str, Any]


class RunOut(BaseModel):
    id: str
    mode: str
    strategy_id: str | None
    strategy_version: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    config: dict[str, Any]
    metrics: dict[str, Any] | None


class EquityPoint(BaseModel):
    ts: datetime
    equity: float
    cash: float
    unrealized_pnl: float


class OrderOut(BaseModel):
    id: str
    ts: datetime
    pair: str
    side: str
    type: str
    size: float
    limit_price: float | None
    status: str
    filled_price: float | None
    filled_ts: datetime | None
    fee: float
    reason: str


class FillOut(BaseModel):
    id: str
    order_id: str
    ts: datetime
    pair: str
    side: str
    price: float
    size: float
    fee: float


class PositionOut(BaseModel):
    pair: str
    size: float
    avg_entry: float
    last_price: float | None
    market_value: float | None


class CandleOut(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BacktestIn(BaseModel):
    strategy: str
    pair: str
    timeframe: str = "1h"
    start: datetime | None = None
    end: datetime | None = None
    days: int = 365
    params: dict[str, Any] = {}
    starting_cash: float = Field(default=10_000.0, gt=0)


class BacktestAccepted(BaseModel):
    run_id: str
    status: Literal["queued"] = "queued"


class ReportOut(BaseModel):
    period: str
    generated_at: datetime
    runs: list[dict[str, Any]]
    totals: dict[str, Any]


class ControlIn(BaseModel):
    run_id: str | None = None  # null = all runs


class ControlOut(BaseModel):
    command: str
    run_id: str | None
    issued_at: datetime


class EventOut(BaseModel):
    id: str
    ts: datetime
    level: str
    source: str
    message: str
    data: dict[str, Any] | None
