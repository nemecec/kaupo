"""Bounded experiment contracts fixed before any candidate executes."""

import json
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from kaupo.domain import Pair, Timeframe, utc_now


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    strategy: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)


class ExperimentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=200)
    strategy_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    mandate: Literal["trend", "portfolio", "maker"]
    hypothesis: str = Field(min_length=20, max_length=4000)
    changed_factor: str = Field(min_length=10, max_length=1000)
    rejection_rule: str = Field(min_length=20, max_length=4000)
    variants: list[Variant] = Field(min_length=2, max_length=12)
    exchange: Literal["kraken", "binance"]
    pairs: list[str] = Field(min_length=1, max_length=12)
    timeframe: Literal["1h", "4h", "1d"]
    start: AwareDatetime
    end: AwareDatetime
    starting_cash: float = Field(default=10000, gt=0, le=10000)

    @model_validator(mode="after")
    def validate_contract(self) -> "ExperimentIn":
        ids = [v.id for v in self.variants]
        if len(set(ids)) != len(ids) or "control" not in ids:
            raise ValueError("variant ids must be unique and include control")
        if not 0 < (self.end - self.start).total_seconds() <= 3660 * 86400:
            raise ValueError("use a fixed historical window of at most ten years")
        if self.end > utc_now():
            raise ValueError("the historical window must end in the past")
        step = Timeframe.parse(self.timeframe).seconds
        if any(ts.timestamp() % step for ts in (self.start, self.end)):
            raise ValueError("start and end must align with candle boundaries")
        parsed = [str(Pair.parse(p)) for p in self.pairs]
        if len(set(parsed)) != len(parsed) or any(not p.endswith("/EUR") for p in parsed):
            raise ValueError("use distinct EUR pairs")
        self.pairs = sorted(parsed)
        if len(json.dumps(self.model_dump(mode="json"), allow_nan=False)) > 40000:
            raise ValueError("experiment contract exceeds 40 KB")
        return self


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_run_id: int = Field(gt=0)
    outcomes: list[dict[str, Any]] = Field(min_length=2, max_length=12)

    @model_validator(mode="after")
    def bounded(self) -> "ResultIn":
        if len(json.dumps(self.model_dump(), allow_nan=False)) > 200000:
            raise ValueError("summary exceeds 200 KB; preserve full evidence in the workflow artifact")
        return self
