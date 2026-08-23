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
from typing import ClassVar, Protocol

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
    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]: ...


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

    def create(self, params: dict) -> StrategyBase:
        validated = self.cls.params_schema.model_validate(params or {})
        return self.cls(validated)
