"""Resolve exact order trade references and compare reported executed volumes."""

from decimal import Decimal, localcontext
from typing import Any

import ccxt.async_support as ccxt

from kaupo.venues.xstocks_fills import _check_fill
from kaupo.venues.xstocks_ledger_check import _decimal
from kaupo.venues.xstocks_order_status import _check_status


def order_trade_owners(orders: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for order_id, order in orders.items():
        _check_status(order)
        references = order.get("trades", [])
        if not references and _decimal(order["vol_exec"]) != 0:
            raise ValueError("An executed order has no trade references")
        for trade_id in references:
            if "," in trade_id or any(c.isspace() for c in trade_id):
                raise ValueError("Invalid order trade reference")
            if trade_id in owners:
                raise ValueError("A trade reference belongs to multiple orders")
            owners[trade_id] = order_id
            if len(owners) > 2000:
                raise ValueError("Order fill inspection exceeds 2000 trade IDs")
    return owners


async def read_order_fills(
    api_key: str, api_secret: str, *, units: str, orders: dict[str, Any]
) -> dict[str, Any]:
    if units not in ("base", "rebased"):
        raise ValueError("Fill units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    owners = order_trade_owners(orders)
    if not owners:
        return {}
    ids = list(owners)
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    fills: dict[str, Any] = {}
    try:
        for offset in range(0, len(ids), 20):
            batch = ids[offset : offset + 20]
            try:
                response = await exchange.private_post_querytrades(
                    {
                        "txid": ",".join(batch),
                        "rebase_multiplier": units,
                        "trades": "false",
                        "ledgers": "true",
                    }
                )
            except Exception as exc:
                raise ValueError(f"Kraken order-fill request failed ({type(exc).__name__})") from None
            if not isinstance(response, dict) or response.get("error") != []:
                raise ValueError("Kraken rejected the order-fill request")
            result = response.get("result")
            if not isinstance(result, dict) or set(result) != set(batch):
                raise ValueError("Kraken returned missing or unexpected order fill IDs")
            for trade_id, fill in result.items():
                _check_fill(fill, start=-1, end=253402300799)
                if fill["ordertxid"] != owners[trade_id]:
                    raise ValueError("Kraken returned a fill for a different order")
                if fill["type"] != orders[owners[trade_id]]["descr"]["type"]:
                    raise ValueError("Kraken returned a fill with a different side")
            fills.update(result)
        return fills
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken order-fill client") from None


def check_order_fill_volumes(orders: dict[str, Any], fills: dict[str, Any]) -> dict[str, Any]:
    owners = order_trade_owners(orders)
    if set(fills) != set(owners):
        raise ValueError("Fill volume checks require exactly the referenced trade IDs")
    reports = {}
    for order_id, order in orders.items():
        try:
            with localcontext() as context:
                context.prec = 128
                total = Decimal(0)
                for trade_id in order.get("trades", []):
                    fill = fills[trade_id]
                    if fill.get("ordertxid") != order_id or fill.get("type") != order["descr"]["type"]:
                        raise ValueError("fill_identity_mismatch")
                    volume = _decimal(fill.get("vol"))
                    if volume <= 0:
                        raise ValueError("invalid_fill_volume")
                    total += volume
                executed = _decimal(order["vol_exec"])
                reports[order_id] = {
                    "status": "volume_match" if total == executed else "volume_mismatch",
                    "executed_volume": order["vol_exec"],
                    "fill_volume": str(total),
                    "difference": str(total - executed),
                    "trade_count": len(order.get("trades", [])),
                }
        except ValueError as exc:
            reports[order_id] = {"status": "unresolved", "reason": str(exc)}
    return {
        "status": "volumes_match"
        if reports and all(r["status"] == "volume_match" for r in reports.values())
        else "needs_review",
        "scope": "referenced_order_fills",
        "orders": reports,
    }
