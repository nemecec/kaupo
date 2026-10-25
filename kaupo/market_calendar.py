"""Explicit local trading hours. No exchange calendar is inferred from symbols."""

import math
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Annotated[int, Field(ge=0, le=6)]


class SessionHours(BaseModel):
    """One local day's open interval, in minutes after midnight; end is exclusive."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0, lt=1440, strict=True)
    end: int = Field(gt=0, le=1440, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must precede end; split overnight sessions at midnight")
        return self


class TradingSchedule(BaseModel):
    """Weekly hours plus complete replacements for individual local dates."""

    model_config = ConfigDict(extra="forbid")

    timezone: str
    week: dict[Weekday, list[SessionHours]]
    overrides: dict[date, list[SessionHours]] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value

    @model_validator(mode="after")
    def disjoint_sessions(self) -> Self:
        if not any(self.week.values()):
            raise ValueError("week must contain at least one open session")
        for sessions in [*self.week.values(), *self.overrides.values()]:
            sessions.sort(key=lambda session: session.start)
            if any(left.end > right.start for left, right in pairwise(sessions)):
                raise ValueError("sessions on the same date must not overlap")
        return self

    def open_elapsed(self, start: datetime, end: datetime, *, candle_seconds: int | None = None) -> timedelta:
        """UTC time in open intervals, optionally expanded to UTC candle boundaries."""
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if end <= start:
            return timedelta(0)
        zone = ZoneInfo(self.timezone)
        # Include adjacent local dates: their sessions can overlap a UTC daily
        # candle even when the session itself falls outside [start, end].
        padding = timedelta(seconds=candle_seconds or 0)
        day = (start - padding).astimezone(zone).date()
        last_day = (end + padding).astimezone(zone).date()
        elapsed = timedelta(0)
        covered_until = start
        while day <= last_day:
            midnight = datetime.combine(day, time())
            for session in self.overrides.get(day, self.week.get(day.weekday(), [])):
                opened = _boundary(midnight + timedelta(minutes=session.start), zone, closing=False)
                closed = _boundary(midnight + timedelta(minutes=session.end), zone, closing=True)
                if candle_seconds is not None:
                    opened = datetime.fromtimestamp(
                        math.floor(opened.timestamp() / candle_seconds) * candle_seconds, UTC
                    )
                    closed = datetime.fromtimestamp(
                        math.ceil(closed.timestamp() / candle_seconds) * candle_seconds, UTC
                    )
                # Expanded sessions can share a candle. Count the union once.
                elapsed += max(timedelta(0), min(end, closed) - max(covered_until, opened))
                covered_until = max(covered_until, closed)
            day += timedelta(days=1)
        return elapsed


def _boundary(local: datetime, zone: ZoneInfo, *, closing: bool) -> datetime:
    # Repeated wall times: open at the first occurrence, close at the last.
    # Missing wall times: move forward by the DST gap, as zoneinfo fold=0 does.
    candidates = [local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [
        candidate for candidate in candidates if candidate.astimezone(zone).replace(tzinfo=None) == local
    ]
    if not valid:
        return max(candidates)
    return max(valid) if closing else min(valid)
