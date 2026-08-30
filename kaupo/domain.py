"""Core domain types shared by all execution modes.

Market data and indicator math use floats; the ledger converts to Decimal
for accounting. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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


@dataclass(frozen=True)
class OpenInterest:
    """Perpetual-futures open interest snapshot for a base asset (e.g. "BTC").

    Kaupo trades spot only; open interest is an advisory positioning signal
    (leverage build-up and unwind), never a traded instrument. Keyed by base
    asset: one dominant USDT perpetual per venue is enough for a signal.
    """

    exchange: str
    base_asset: str
    ts: datetime  # snapshot time, UTC
    oi_base: float  # open interest in base units (contracts)
    oi_quote: float  # open interest in quote units (USD notional)


@dataclass(frozen=True)
class TradeTick:
    """One public trade print (tick) from an exchange.

    Order-flow data: who traded how much in which direction. Kraken serves
    no trade id for public trades, so identity is the full
    (exchange, pair, ts, price, size, side) tuple.
    """

    exchange: str
    pair: str  # unified pair string, e.g. "BTC/EUR"
    ts: datetime  # trade time, UTC (ms precision — ccxt truncates)
    price: float
    size: float  # in base currency
    side: str  # "buy" | "sell" (taker side)


@dataclass(frozen=True)
class BookSnapshot:
    """One top-of-book observation (best bid/ask with sizes) of a pair.

    Input for maker-fill fidelity analysis and spread/depth features. No
    public API serves historical books, so rows come from forward polling
    only; identity is (exchange, pair, ts), so an unchanged book top between
    two polls dedupes to one row.
    """

    exchange: str
    pair: str  # unified pair string, e.g. "BTC/EUR"
    ts: datetime  # observation time, UTC (ticker time; poll time as fallback)
    bid: float
    ask: float
    bid_size: float  # in base currency; 0 when the venue serves no size
    ask_size: float


@dataclass(frozen=True)
class TickFlow:
    """Order-flow aggregate of one candle bucket of trade ticks.

    Derived from stored :class:`TradeTick` rows bucketed per candle: trade
    counts and base-currency volumes per taker side, plus the largest single
    trade. Inherits tick coverage: only pairs the tick collector feeds, kept
    for a rolling 30 days.
    """

    ts: datetime  # bucket (candle) open time, UTC
    buy_count: int
    sell_count: int
    buy_volume: float  # in base currency
    sell_volume: float  # in base currency
    max_trade_size: float  # largest single trade of the bucket, in base currency


@dataclass(frozen=True)
class OrderflowDaily:
    """Permanent daily order-flow aggregate of one pair (one UTC day).

    Rolled up daily from the raw :class:`TradeTick` and :class:`BookSnapshot`
    stores: trade counts and base-currency volumes per taker side, the
    largest single trade, the day's book-snapshot count, and the spread
    statistics in basis points (null when no book was collected that day).
    The raw stores are retention-capped at a rolling 30 days; these
    aggregates are never pruned and accumulate forward from 2026-08-28.
    """

    exchange: str
    pair: str  # unified pair string, e.g. "BTC/EUR"
    day: date  # the UTC day the row aggregates
    trade_count: int
    buy_count: int
    sell_count: int
    buy_volume: float  # in base currency
    sell_volume: float  # in base currency
    max_trade_size: float  # largest single trade of the day, in base currency
    book_snapshots: int  # 0 when the book collector served nothing that day
    spread_mean_bps: float | None  # mean of (ask-bid)/mid*10000 over the day's snapshots
    spread_max_bps: float | None  # max of the same; both null when book_snapshots is 0


@dataclass(frozen=True)
class FuturesMetricsDaily:
    """Daily futures positioning metrics of one base asset (one UTC day).

    Aggregated from the Binance USD-M perp metrics archive (5-minute rows):
    open interest is the end-of-day snapshot, the long/short ratios are day
    means. Tracks market-wide leverage positioning (advisory signal, never a
    traded instrument). Never pruned.
    """

    exchange: str
    base_asset: str  # e.g. "BTC" (the USDT perp's base)
    day: date  # the UTC day the row aggregates
    oi_base: float  # end-of-day open interest in base units
    oi_quote: float  # end-of-day open interest in USD notional
    count_toptrader_ls_ratio: float  # day mean of top-trader accounts long/short ratio
    sum_toptrader_ls_ratio: float  # day mean of top-trader positions long/short ratio
    count_ls_ratio: float  # day mean of all-accounts long/short ratio
    taker_ls_vol_ratio: float  # day mean of taker buy/sell volume ratio


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
