"""Core domain types shared by all execution modes.

Market data and indicator math use floats; the ledger converts to Decimal
for accounting. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NewType
from uuid import uuid4

RunId = NewType("RunId", str)
OrderId = NewType("OrderId", str)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class Timeframe(enum.Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return {
            Timeframe.M1: 60,
            Timeframe.M5: 300,
            Timeframe.M15: 900,
            Timeframe.M30: 1800,
            Timeframe.H1: 3600,
            Timeframe.H4: 14400,
            Timeframe.D1: 86400,
        }[self]

    @property
    def periods_per_year(self) -> float:
        return 365.25 * 86400 / self.seconds

    @classmethod
    def parse(cls, value: str) -> Timeframe:
        try:
            return cls(value)
        except ValueError:
            valid = ", ".join(t.value for t in cls)
            raise ValueError(f"Unknown timeframe {value!r}; valid: {valid}") from None


@dataclass(frozen=True)
class Pair:
    """Unified ccxt-style pair, e.g. ``BTC/EUR``."""

    base: str
    quote: str

    @classmethod
    def parse(cls, value: str) -> Pair:
        base, sep, quote = value.upper().partition("/")
        if not sep or not base or not quote:
            raise ValueError(f"Invalid pair {value!r}; expected BASE/QUOTE, e.g. BTC/EUR")
        return cls(base=base, quote=quote)

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass(frozen=True)
class Candle:
    pair: Pair
    timeframe: Timeframe
    ts: datetime  # open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    exchange: str = "kraken"  # source venue; part of the storage key


@dataclass(frozen=True)
class FundingRate:
    """Perpetual-futures funding rate for a base asset (e.g. "BTC").

    Kaupo trades spot only; funding is an advisory filter signal (crowded
    positioning), never a traded instrument. Keyed by base asset: one
    dominant USDT perpetual per venue is enough for a filter.
    """

    exchange: str
    base_asset: str
    ts: datetime  # funding time, UTC
    rate: float  # per funding interval, as a fraction (0.0001 = 1 bps)


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(enum.Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RunMode(enum.Enum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    LIVE = "live"


class RunStatus(enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    HALTED = "halted"  # stopped by risk manager or kill switch
    FAILED = "failed"


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants to do. The risk manager and venue decide what happens.

    MARKET intents fill at the next candle's open (taker fee + slippage).
    LIMIT intents need a positive ``limit_price`` and live for one candle:
    they fill at the limit or better when that candle's range touches the
    price (maker fee, no slippage) and expire unfilled at its close.
    """

    pair: Pair
    side: Side
    size: float  # in base currency
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError(f"Order size must be positive, got {self.size}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError(f"Limit orders require a positive limit_price, got {self.limit_price}")
        elif self.limit_price is not None:
            raise ValueError("Market orders must not set limit_price")


@dataclass
class Order:
    pair: Pair
    side: Side
    order_type: OrderType
    size: float
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    id: OrderId = field(default_factory=lambda: OrderId(new_id()))
    status: OrderStatus = OrderStatus.OPEN
    created_ts: datetime = field(default_factory=utc_now)
    filled_price: float | None = None
    filled_ts: datetime | None = None
    fee: float = 0.0


@dataclass(frozen=True)
class Fill:
    order_id: OrderId
    pair: Pair
    side: Side
    ts: datetime
    price: float
    size: float
    fee: float

    @property
    def quote_amount(self) -> float:
        return self.price * self.size


@dataclass
class Position:
    """Spot position in one pair: base size held and average entry price."""

    pair: Pair
    size: float = 0.0
    avg_entry: float = 0.0

    def market_value(self, price: float) -> float:
        return self.size * price
