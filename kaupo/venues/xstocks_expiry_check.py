"""Compare a single order observation with an operator-supplied expiry deadline."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kaupo.venues.xstocks_ledger_check import _decimal
from kaupo.venues.xstocks_order_status import _check_status


def check_expiry_request(order_ids: list[str], expected_expiry: int) -> None:
    if len(order_ids) != 1:
        raise ValueError("Expiry checks require exactly one order ID")
    if (
        isinstance(expected_expiry, bool)
        or not isinstance(expected_expiry, int)
        or not 0 < expected_expiry <= 253402300799
    ):
        raise ValueError("Expected expiry must be a positive Unix timestamp through year 9999")


def _seconds(value: datetime) -> Decimal:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid_observation_interval")
    elapsed = value - datetime(1970, 1, 1, tzinfo=UTC)
    return Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(elapsed.microseconds) / 1000000


def check_order_expiry(
    order: dict[str, Any], *, expected_expiry: int, started_at: datetime, completed_at: datetime
) -> dict[str, Any]:
    check_expiry_request(["order"], expected_expiry)
    report: dict[str, Any] = {"expected_expiry": expected_expiry, "deadline_source": "operator"}
    try:
        _check_status(order)
        start, end = _seconds(started_at), _seconds(completed_at)
        if end < start:
            raise ValueError("invalid_observation_interval")
        expiry = _decimal(str(order["expiretm"]))
        opened = _decimal(str(order["opentm"]))
        report.update(
            recorded_expiry=order["expiretm"], order_status=order["status"], executed_volume=order["vol_exec"]
        )
        if order["descr"]["ordertype"] != "limit":
            raise ValueError("unsupported_order_type")
        if expiry != expected_expiry:
            raise ValueError("expiry_mismatch")
        if opened > end:
            raise ValueError("order_time_after_observation")
        if opened > expected_expiry:
            raise ValueError("opened_after_deadline")
        if order["status"] in ("closed", "canceled", "expired"):
            closed = _decimal(str(order["closetm"]))
            report["closed_at"] = order["closetm"]
            if closed > end:
                raise ValueError("order_time_after_observation")
            if closed > expected_expiry:
                raise ValueError("terminal_after_deadline")
            if order["status"] == "expired" and closed < expected_expiry:
                raise ValueError("expired_before_deadline")
            return {**report, "status": "terminal_by_deadline"}
        if start > expected_expiry:
            raise ValueError("active_after_deadline")
        if end >= expected_expiry:
            raise ValueError("observation_overlaps_deadline")
        raise ValueError("deadline_not_reached")
    except ValueError as exc:
        return {**report, "status": "needs_review", "reason": str(exc)}
