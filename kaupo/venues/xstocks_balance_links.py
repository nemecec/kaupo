"""Check arithmetic links in selected ledgers, without claiming account completeness."""

from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Any

from kaupo.venues.xstocks_ledger_check import _decimal


def _asset_links(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    ordered: list[tuple[Decimal, str, dict[str, Any]]] = []
    for ledger_id, entry in rows:
        timestamp = entry.get("time")
        if isinstance(timestamp, bool) or not isinstance(timestamp, str | int | float):
            raise ValueError("invalid_timestamp")
        time = _decimal(str(timestamp))
        if time < 0:
            raise ValueError("invalid_timestamp")
        for field in ("amount", "fee", "balance"):
            _decimal(entry.get(field))
        ordered.append((time, ledger_id, entry))
    if len({time for time, _, _ in ordered}) != len(ordered):
        raise ValueError("ambiguous_timestamp_order")
    ordered.sort(key=lambda row: row[0])
    links = []
    with localcontext() as context:
        context.prec = 128
        for previous, current in pairwise(ordered):
            expected = (
                _decimal(previous[2]["balance"])
                + _decimal(current[2]["amount"])
                - _decimal(current[2]["fee"])
            )
            actual = _decimal(current[2]["balance"])
            links.append(
                {
                    "previous_id": previous[1],
                    "current_id": current[1],
                    "expected_balance": str(expected),
                    "actual_balance": str(actual),
                    "difference": str(actual - expected),
                    "status": "match" if actual == expected else "gap",
                }
            )
    status = "links_match" if links else "insufficient_entries"
    if any(link["status"] == "gap" for link in links):
        status = "gaps_detected"
    return {"status": status, "anchor_id": ordered[0][1], "last_id": ordered[-1][1], "links": links}


def check_balance_links(ledgers: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for ledger_id, entry in ledgers.items():
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(field), str) or not entry[field].strip() for field in ("asset", "aclass")
        ):
            raise ValueError("Invalid ledger asset identity for balance links")
        groups[(entry["aclass"], entry["asset"])].append((ledger_id, entry))
    assets = []
    for (asset_class, asset), rows in sorted(groups.items()):
        try:
            report = _asset_links(rows)
        except ValueError as exc:
            report = {"status": "unresolved", "reason": str(exc)}
        assets.append({"asset_class": asset_class, "asset": asset, "entry_count": len(rows), **report})
    counts = Counter(row["status"] for row in assets)
    status = "no_ledgers" if not assets else "links_match"
    if any(row["status"] != "links_match" for row in assets):
        status = "needs_review"
    return {"status": status, "scope": "selected_ledgers", "counts": dict(counts), "assets": assets}
