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

from kaupo.domain import Candle, FundingRate, OrderIntent, Pair, Position


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
