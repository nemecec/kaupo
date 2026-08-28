"""Strategy plugin contract.

A strategy is a class deriving from :class:`StrategyBase` (single pair) or
:class:`PortfolioStrategyBase` (multi-pair universe, backtest and shadow)
with:

- ``id``: unique strategy identifier
- ``params_schema``: a pydantic model class; the engine validates user params
  against it before instantiating the strategy
- ``on_candle(ctx)``: called once per closed candle (single pair) or once
  per joined timestamp step (portfolio); returns order intents

Determinism rules (enforced by ``kaupo lint-strategies``):

- no wall-clock access — use ``ctx.clock.now()`` (virtual in backtests)
- no I/O — no files, network, or subprocesses
- no unseeded randomness — declare state in instance attributes
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from kaupo.domain import (
    BookSnapshot,
    Candle,
    FundingRate,
    OrderflowDaily,
    OrderIntent,
    Pair,
    Position,
    TickFlow,
    TradeTick,
)


class EmptyParams(BaseModel):
    pass


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time. Virtual (candle-driven) in backtests, real in live."""
        ...


class StrategyContext(Protocol):
    """Read-only view of the world handed to a strategy on each candle."""

    @property
    def clock(self) -> Clock: ...

    @property
    def candle(self) -> Candle:
        """The just-closed candle."""
        ...

    def history(self, n: int) -> Sequence[Candle]:
        """Last ``n`` closed candles including the current one, oldest first.

        Returns fewer than ``n`` at the start of a run.
        """
        ...

    def funding(self, n: int) -> Sequence[FundingRate]:
        """Latest ``n`` funding points for the run pair's base asset, oldest first.

        Point-in-time like ``history``: only funding with funding time at or
        before ``clock.now()`` is ever returned, in backtests and live alike.
        Empty when no funding was ingested for the base asset (funding is an
        advisory signal sourced from Binance USDT perpetuals).
        """
        ...

    def ticks(self, n: int) -> Sequence[TradeTick]:
        """Latest ``n`` trade ticks for the run pair, oldest first.

        Point-in-time like ``history``: only ticks with trade time at or
        before ``clock.now()`` are ever returned, in backtests and live
        alike. Empty when no ticks were stored for the pair — ticks exist
        only for the pairs the tick collector feeds (Kraken majors) and are
        kept for a rolling 30 days, so absence is normal: strategies must
        tolerate an empty series, and backtests older than the retention
        window simply see nothing.
        """
        ...

    def book(self, n: int) -> Sequence[BookSnapshot]:
        """Latest ``n`` top-of-book snapshots for the run pair, oldest first.

        Point-in-time like ``history``: only snapshots with observation time
        at or before ``clock.now()`` are ever returned, in backtests and live
        alike. Empty when no snapshots were stored for the pair — book data
        exists only for the pairs the book collector feeds and is kept for a
        rolling 30 days, so strategies must tolerate absence.
        """
        ...

    def tick_flow(self, n: int) -> Sequence[TickFlow]:
        """Trade ticks bucketed per candle of the run's timeframe, oldest first.

        One :class:`TickFlow` (buy/sell counts and volumes, largest trade)
        per candle that saw at least one trade, over the newest ``n``
        completed candles; candles without trades are absent. Only buckets
        fully closed at or before ``clock.now()`` are returned — the
        in-progress candle never leaks. Empty when no tick data (see
        ``ticks`` for the coverage and retention boundary).
        """
        ...

    def tick_flow_daily(self, n: int) -> Sequence[OrderflowDaily]:
        """Latest ``n`` permanent daily order-flow aggregates for the run pair.

        One :class:`OrderflowDaily` per UTC day: trade counts and volumes
        per taker side, the largest trade, the book-snapshot count, and the
        day's spread statistics. Point-in-time like ``history``: only days
        fully closed at ``clock.now()`` are returned — the in-progress day
        never leaks. Where the raw ticks and book snapshots keep a rolling
        30 days, these daily aggregates are permanent: they accumulate
        forward from 2026-08-28, and days before that (or without raw
        coverage) are absent. Empty when no aggregates exist for the pair,
        so strategies must tolerate an empty series.
        """
        ...

    def position(self) -> Position:
        """Current position for the run's pair (size 0 when flat)."""
        ...

    def cash(self) -> float:
        """Available quote currency."""
        ...

    def equity(self) -> float:
        """cash + position market value at the current close."""
        ...


class StrategyBase(ABC):
    id: ClassVar[str]
    params_schema: ClassVar[type[BaseModel]] = EmptyParams

    def __init__(self, params: BaseModel) -> None:
        self.params = params

    @abstractmethod
    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        """Return this candle's intents ([] to do nothing); risk may resize or reject.

        MARKET intents fill at the next candle's open (taker fee + slippage).
        LIMIT intents set a positive ``limit_price`` and live for one candle:
        they fill at the limit or better when the range touches the price
        (maker fee, no slippage) and expire unfilled at that candle's close.
        """
        ...


class PortfolioContext(Protocol):
    """Read-only view of the world handed to a portfolio strategy on each step.

    One step is one timestamp of the joined universe: only the pairs with a
    candle closed at that timestamp appear in ``candles``.
    """

    @property
    def clock(self) -> Clock: ...

    @property
    def candles(self) -> Mapping[Pair, Candle]:
        """The candles closed this step, by pair. A pair without a candle
        this step is absent — its last known close still feeds equity."""
        ...

    def history(self, pair: Pair, n: int) -> Sequence[Candle]:
        """Last ``n`` closed candles for ``pair`` including this step's, oldest first.

        Returns fewer than ``n`` at the start of a run. A pair's history
        advances only on steps where that pair has a candle.
        """
        ...

    def funding(self, pair: Pair, n: int) -> Sequence[FundingRate]:
        """Latest ``n`` funding points for ``pair``'s base asset, oldest first.

        Point-in-time like ``history``: only funding with funding time at or
        before ``clock.now()`` is ever returned. Empty when no funding was
        ingested for the base asset (advisory signal, Binance USDT perpetuals).
        """
        ...

    def ticks(self, pair: Pair, n: int) -> Sequence[TradeTick]:
        """Latest ``n`` trade ticks for ``pair``, oldest first.

        Point-in-time like ``history``: only ticks with trade time at or
        before ``clock.now()`` are ever returned, in backtests and live
        alike. Empty for pairs outside the universe and when no ticks were
        stored — ticks exist only for the pairs the tick collector feeds
        (Kraken majors) and are kept for a rolling 30 days, so strategies
        must tolerate absence.
        """
        ...

    def book(self, pair: Pair, n: int) -> Sequence[BookSnapshot]:
        """Latest ``n`` top-of-book snapshots for ``pair``, oldest first.

        Point-in-time like ``history``: only snapshots with observation time
        at or before ``clock.now()`` are ever returned, in backtests and live
        alike. Empty for pairs outside the universe and when no snapshots
        were stored — book data exists only for the pairs the book collector
        feeds and is kept for a rolling 30 days.
        """
        ...

    def tick_flow(self, pair: Pair, n: int) -> Sequence[TickFlow]:
        """Trade ticks of ``pair`` bucketed per candle of the run's timeframe.

        One :class:`TickFlow` (buy/sell counts and volumes, largest trade)
        per candle that saw at least one trade, over the newest ``n``
        completed candles, oldest first; candles without trades are absent.
        Only buckets fully closed at or before ``clock.now()`` are returned —
        the in-progress candle never leaks. Empty when no tick data (see
        ``ticks`` for the coverage and retention boundary).
        """
        ...

    def tick_flow_daily(self, pair: Pair, n: int) -> Sequence[OrderflowDaily]:
        """Latest ``n`` permanent daily order-flow aggregates for ``pair``.

        One :class:`OrderflowDaily` per UTC day: trade counts and volumes
        per taker side, the largest trade, the book-snapshot count, and the
        day's spread statistics, oldest first. Point-in-time like
        ``history``: only days fully closed at ``clock.now()`` are returned —
        the in-progress day never leaks. Where the raw ticks and book
        snapshots keep a rolling 30 days, these daily aggregates are
        permanent: they accumulate forward from 2026-08-28, and days before
        that (or without raw coverage) are absent. Empty for pairs outside
        the universe and when no aggregates exist for the pair.
        """
        ...

    def positions(self) -> Mapping[Pair, Position]:
        """Open positions by pair (only pairs with a nonzero size)."""
        ...

    def cash(self) -> float:
        """Available quote currency."""
        ...

    def equity(self) -> float:
        """cash + every position valued at its last known close."""
        ...


class PortfolioStrategyBase(ABC):
    """Contract for multi-pair strategies (backtest and shadow modes)."""

    id: ClassVar[str]
    params_schema: ClassVar[type[BaseModel]] = EmptyParams

    def __init__(self, params: BaseModel) -> None:
        self.params = params

    @abstractmethod
    def on_candle(self, ctx: PortfolioContext) -> list[OrderIntent]:
        """Return this step's intents ([] to do nothing); risk may resize or reject.

        Fill semantics match the single-pair engine, per pair: MARKET intents
        fill at the pair's next candle open (taker fee + slippage); LIMIT
        intents live for one candle of that pair (maker fee, no slippage).
        Every intent must name a pair of the run's universe; intents for
        foreign pairs are rejected.
        """
        ...


@dataclass(frozen=True)
class LoadedStrategy:
    """A strategy class discovered on disk, with provenance."""

    id: str
    cls: type[StrategyBase] | type[PortfolioStrategyBase]
    source_hash: str  # sha256 of the source file
    path: str

    @property
    def version(self) -> str:
        return self.source_hash[:12]

    @property
    def is_portfolio(self) -> bool:
        return issubclass(self.cls, PortfolioStrategyBase)

    def create(self, params: dict[str, Any]) -> StrategyBase | PortfolioStrategyBase:
        params = params or {}
        # allowed keys = field names plus aliases (honoring populate_by_name)
        schema = self.cls.params_schema
        allowed: set[str] = set()
        populate_by_name = schema.model_config.get("populate_by_name", False)
        for name, field_info in schema.model_fields.items():
            if field_info.alias is not None:
                allowed.add(field_info.alias)
                if populate_by_name:
                    allowed.add(name)
            else:
                allowed.add(name)
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(
                f"Unknown params for strategy {self.id!r}: {unknown} (allowed: {sorted(allowed)})"
            )
        validated = schema.model_validate(params)
        return self.cls(validated)
