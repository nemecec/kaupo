"""Compare order and fill instruments through exact current public metadata aliases."""

from typing import Any

from kaupo.data.xstocks_identity import identity_maps
from kaupo.venues.xstocks_order_fills import order_trade_owners


def check_order_fill_identities(
    orders: dict[str, Any], fills: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    owners = order_trade_owners(orders)
    if set(fills) != set(owners):
        raise ValueError("Order identity checks require exactly the referenced trade IDs")
    pairs, _, _ = identity_maps(metadata)
    reports = {}
    for order_id, order in orders.items():
        symbol = order["descr"]["pair"]
        pair = pairs.get(symbol)
        checks = {}
        for trade_id in order.get("trades", []):
            fill = fills[trade_id]
            fill_pair = pairs.get(fill["pair"])
            reason = None
            if fill.get("ordertxid") != order_id:
                reason = "fill_order_mismatch"
            elif pair is None:
                reason = "unmapped_order_pair"
            elif fill_pair is None:
                reason = "unmapped_fill_pair"
            elif pair != fill_pair:
                reason = "order_fill_pair_mismatch"
            checks[trade_id] = {
                "status": "needs_review" if reason else "pair_match",
                "raw_pair": fill["pair"],
                "mapped_pair": fill_pair,
                **({"reason": reason} if reason else {}),
            }
        status = "pairs_match" if checks else "no_fills"
        if any(row["status"] == "needs_review" for row in checks.values()):
            status = "needs_review"
        reports[order_id] = {"status": status, "raw_pair": symbol, "mapped_pair": pair, "fills": checks}
    status = "pairs_match" if fills else "no_fills"
    if any(row["status"] == "needs_review" for row in reports.values()):
        status = "needs_review"
    return {"status": status, "scope": "referenced_order_fills", "orders": reports}
