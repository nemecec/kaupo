"""API request/response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from kaupo.backtest.sweep import validate_sweep_spec


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


class BenchmarkPoint(BaseModel):
    ts: datetime
    value: float


class RunEquityOut(BaseModel):
    """Per-run equity curve plus its buy-and-hold benchmark (same timestamps)."""

    points: list[EquityPoint]
    benchmark: list[BenchmarkPoint]  # empty when no candles cover the run window


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


class FundingOut(BaseModel):
    exchange: str
    base_asset: str
    ts: datetime
    rate: float


class OpenInterestOut(BaseModel):
    exchange: str
    base_asset: str
    ts: datetime
    oi_base: float
    oi_quote: float


class FuturesMetricsDailyOut(BaseModel):
    exchange: str
    base_asset: str
    day: date
    oi_base: float
    oi_quote: float
    count_toptrader_ls_ratio: float | None
    sum_toptrader_ls_ratio: float | None
    count_ls_ratio: float | None
    taker_ls_vol_ratio: float | None


class TradeTickOut(BaseModel):
    exchange: str
    pair: str
    ts: datetime
    price: float
    size: float
    side: str


class BookSnapshotOut(BaseModel):
    exchange: str
    pair: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float


class OrderflowDailyOut(BaseModel):
    exchange: str
    pair: str
    day: date
    trade_count: int
    buy_count: int
    sell_count: int
    buy_volume: float
    sell_volume: float
    max_trade_size: float
    book_snapshots: int
    spread_mean_bps: float | None
    spread_max_bps: float | None


class BacktestIn(BaseModel):
    strategy: str
    pair: str | None = None  # single-pair backtest
    pairs: list[str] | None = None  # portfolio backtest (>=2 pairs, one shared quote)
    timeframe: str = "1h"
    start: datetime | None = None
    end: datetime | None = None
    days: int = Field(default=365, ge=1)
    params: dict[str, Any] = {}
    starting_cash: float = Field(default=10_000.0, gt=0)
    exchange: str = "kraken"
    instrument: Literal["spot", "perp"] = "spot"  # perp: shorts at 1x, funding charged
    # when set, the worker also runs K equal time slices of [start, end]
    # and the job result carries per-window metrics (overfitting check)
    stability_windows: int | None = Field(default=None, ge=2, le=12)
    # when set, the worker runs one backtest per point of the grid (the
    # cartesian product, <= 50 points) and the job result carries per-point
    # metrics; keys are strategy param names, values scalars; not combinable
    # with stability_windows
    sweep: dict[str, list[Any]] | None = None
    # research overrides for the backtest risk caps; null keeps the live defaults
    max_position_quote: float | None = Field(default=None, gt=0)
    max_gross_exposure_quote: float | None = Field(default=None, gt=0)
    max_daily_loss_quote: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> BacktestIn:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        if (self.pair is None) == (self.pairs is None):
            raise ValueError("pass exactly one of pair or pairs")
        if self.pairs is not None and len(self.pairs) < 2:
            raise ValueError("pairs needs at least 2 entries; use pair for one pair")
        if self.sweep is not None:
            validate_sweep_spec(self.sweep)
            if self.stability_windows is not None:
                raise ValueError("sweep and stability_windows cannot be combined")
        return self


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


class SettingsIn(BaseModel):
    shadow_strategy: str | None = None
    shadow_pair: str | None = None
    shadow_timeframe: str | None = None


class SettingsOut(BaseModel):
    shadow_strategy: str
    shadow_pair: str
    shadow_timeframe: str
    updated_at: dict[str, datetime]  # per stored key; absent keys use defaults


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


class AssignmentIn(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=100)  # generated when absent
    strategy_id: str
    pair: str | None = None  # single-pair run
    pairs: list[str] | None = None  # portfolio run (>=2 pairs, one shared quote)
    timeframe: str
    mode: str = "shadow"
    params: dict[str, Any] = {}
    enabled: bool = True
    starting_cash: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> AssignmentIn:
        if (self.pair is None) == (self.pairs is None):
            raise ValueError("pass exactly one of pair or pairs")
        if self.pairs is not None and len(self.pairs) < 2:
            raise ValueError("pairs needs at least 2 entries; use pair for one pair")
        return self


class AssignmentUpdate(BaseModel):
    strategy_id: str | None = None
    pair: str | None = None
    pairs: list[str] | None = None  # setting pairs rewrites pair to the joined universe
    timeframe: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    starting_cash: float | None = Field(default=None, gt=0)  # null = leave unchanged

    @model_validator(mode="after")
    def _check(self) -> AssignmentUpdate:
        if self.pair is not None and self.pairs is not None:
            raise ValueError("pass at most one of pair or pairs")
        if self.pairs is not None and len(self.pairs) < 2:
            raise ValueError("pairs needs at least 2 entries; use pair for one pair")
        return self


class AssignmentOut(BaseModel):
    id: str
    strategy_id: str
    pair: str
    pairs: list[str] | None
    timeframe: str
    mode: str
    params: dict[str, Any]
    enabled: bool
    starting_cash: float | None
    created_at: datetime
    updated_at: datetime
    run_id: str | None  # the matching live run; null when not running
