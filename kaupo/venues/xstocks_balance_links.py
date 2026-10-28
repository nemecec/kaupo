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


def check_window_balances(
    ledgers: dict[str, Any], *, asset: str, asset_class: str, opening: str, closing: str
) -> dict[str, Any]:
    """Check a single asset window against operator-supplied boundary balances."""
    initial, final = _decimal(opening), _decimal(closing)
    report: dict[str, Any] = {
        "scope": "asset_window",
        "anchor_source": "operator",
        "opening_balance": opening,
        "closing_balance": closing,
        "entry_count": len(ledgers),
    }
    try:
        for entry in ledgers.values():
            if (
                not isinstance(entry, dict)
                or entry.get("asset") != asset
                or entry.get("aclass") != asset_class
            ):
                raise ValueError("asset_scope_mismatch")
        links = _asset_links(list(ledgers.items())) if ledgers else None
    except ValueError as exc:
        return {**report, "status": "needs_review", "reason": str(exc)}
    with localcontext() as context:
        context.prec = 128
        if links is None:
            checks = [
                {"boundary": "empty_window", "expected_balance": str(initial), "actual_balance": str(final)}
            ]
        else:
            first = ledgers[links["anchor_id"]]
            last = ledgers[links["last_id"]]
            checks = [
                {
                    "boundary": "opening",
                    "ledger_id": links["anchor_id"],
                    "expected_balance": str(initial + _decimal(first["amount"]) - _decimal(first["fee"])),
                    "actual_balance": first["balance"],
                },
                {
                    "boundary": "closing",
                    "ledger_id": links["last_id"],
                    "expected_balance": last["balance"],
                    "actual_balance": str(final),
                },
            ]
        for check in checks:
            difference = Decimal(check["actual_balance"]) - Decimal(check["expected_balance"])
            check.update(difference=str(difference), status="match" if difference == 0 else "gap")
    matches = all(check["status"] == "match" for check in checks)
    if links is not None and links["status"] == "gaps_detected":
        matches = False
    return {
        **report,
        "status": "balances_match" if matches else "needs_review",
        "boundaries": checks,
        "links": links["links"] if links is not None else [],
    }
