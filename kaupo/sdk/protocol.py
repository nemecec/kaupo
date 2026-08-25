"""Strategy plugin contract.

A strategy is a class deriving from :class:`StrategyBase` with:

- ``id``: unique strategy identifier
- ``params_schema``: a pydantic model class; the engine validates user params
  against it before instantiating the strategy
- ``on_candle(ctx)``: called once per closed candle; returns order intents

Determinism rules (enforced by ``kaupo lint-strategies``):

- no wall-clock access — use ``ctx.clock.now()`` (virtual in backtests)
- no I/O — no files, network, or subprocesses
- no unseeded randomness — declare state in instance attributes
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from kaupo.domain import Candle, OrderIntent, Position


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


@dataclass(frozen=True)
class LoadedStrategy:
    """A strategy class discovered on disk, with provenance."""

    id: str
    cls: type[StrategyBase]
    source_hash: str  # sha256 of the source file
    path: str

    @property
    def version(self) -> str:
        return self.source_hash[:12]

    def create(self, params: dict[str, Any]) -> StrategyBase:
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
