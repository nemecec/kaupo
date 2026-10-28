"""Compare referenced fill timestamps with reported order lifetime and intended expiry."""

from typing import Any

from kaupo.venues.xstocks_expiry_check import check_expiry_request
from kaupo.venues.xstocks_ledger_check import _decimal
from kaupo.venues.xstocks_order_fills import order_trade_owners


def check_order_fill_times(
    orders: dict[str, Any], fills: dict[str, Any], *, expected_expiry: int | None = None
) -> dict[str, Any]:
    if expected_expiry is not None:
        check_expiry_request(list(orders), expected_expiry)
    owners = order_trade_owners(orders)
    if set(fills) != set(owners):
        raise ValueError("Fill time checks require exactly the referenced trade IDs")
    reports: dict[str, Any] = {}
    for order_id, order in orders.items():
        rows = {}
        for trade_id in order.get("trades", []):
            fill = fills[trade_id]
            try:
                if not isinstance(fill, dict) or fill.get("ordertxid") != order_id:
                    raise ValueError("fill_order_mismatch")
                value = fill.get("time")
                if isinstance(value, bool) or not isinstance(value, str | int | float):
                    raise ValueError("invalid_fill_timestamp")
                time = _decimal(str(value))
                opened = _decimal(str(order["opentm"]))
                violations = []
                if time < opened:
                    violations.append("fill_before_open")
                if order["status"] in ("closed", "canceled", "expired"):
                    closed = _decimal(str(order["closetm"]))
                    if time > closed:
                        violations.append("fill_after_close")
                if expected_expiry is not None and time > expected_expiry:
                    violations.append("fill_after_expected_expiry")
                rows[trade_id] = {
                    "status": "outside_bounds" if violations else "within_bounds",
                    "time": value,
                    "violations": violations,
                }
            except ValueError as exc:
                rows[trade_id] = {"status": "unresolved", "reason": str(exc)}
        status = "times_match" if rows else "no_fills"
        if any(row["status"] != "within_bounds" for row in rows.values()):
            status = "needs_review"
        reports[order_id] = {
            "status": status,
            "opened_at": order["opentm"],
            **(
                {"closed_at": order["closetm"]}
                if order["status"] in ("closed", "canceled", "expired")
                else {}
            ),
            "fills": rows,
        }
    status = "times_match" if fills else "no_fills"
    if any(report["status"] == "needs_review" for report in reports.values()):
        status = "needs_review"
    return {
        "status": status,
        "scope": "referenced_order_fills",
        **(
            {"expected_expiry": expected_expiry, "deadline_source": "operator"}
            if expected_expiry is not None
            else {}
        ),
        "orders": reports,
    }
