"""Read-only Kraken account evidence with explicit xStocks units."""

from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt.async_support as ccxt


async def read_balances(api_key: str, api_secret: str, *, units: str) -> dict[str, str]:
    """Preserve exchange asset IDs and decimal strings; do not infer spendable funds."""
    if units not in ("rebased", "base"):
        raise ValueError("Balance units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    try:
        try:
            response = await exchange.private_post_balance({"rebase_multiplier": units})
        except Exception as exc:
            raise ValueError(f"Kraken balance request failed ({type(exc).__name__})") from None
        if not isinstance(response, dict) or response.get("error") != []:
            raise ValueError("Kraken rejected the balance request")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("Kraken returned an invalid balance response")
        balances: dict[str, str] = {}
        for asset, value in result.items():
            if not isinstance(asset, str) or not asset.strip() or not isinstance(value, str):
                raise ValueError("Kraken returned an invalid balance entry")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                raise ValueError("Kraken returned an invalid balance amount") from None
            if not amount.is_finite():
                raise ValueError("Kraken returned a non-finite balance amount")
            balances[asset] = value
        return balances
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken balance client") from None


def _check_open_order(order: object) -> None:
    """Check inspection fields without translating symbols or interpreting order rules."""
    if not isinstance(order, dict):
        raise ValueError("Kraken returned an invalid open order")
    description = order.get("descr")
    if not isinstance(description, dict) or any(
        not isinstance(description.get(field), str) or not description[field].strip()
        for field in ("pair", "type", "ordertype")
    ):
        raise ValueError("Kraken returned an invalid order description")
    if order.get("status") not in ("pending", "open"):
        raise ValueError("Kraken returned an unexpected open-order status")
    amounts = []
    for field in ("vol", "vol_exec", "expiretm"):
        value = order.get(field)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError("Kraken returned an invalid order amount or expiry")
        if field != "expiretm" and not isinstance(value, str):
            raise ValueError("Kraken returned a non-string order volume")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("Kraken returned an invalid order amount or expiry") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("Kraken returned an invalid order amount or expiry")
        amounts.append(amount)
    if amounts[0] <= 0 or amounts[1] > amounts[0]:
        raise ValueError("Kraken returned inconsistent order volumes")


async def read_open_orders(api_key: str, api_secret: str, *, units: str) -> dict[str, Any]:
    """Read all cursor pages; never expose a partial collection as successful evidence."""
    if units not in ("rebased", "base"):
        raise ValueError("Order units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    orders: dict[str, Any] = {}
    cursors: set[str] = set()
    params: dict[str, Any] = {
        "rebase_multiplier": units,
        "with_cursor": "true",
        "limit": 100,
        "trades": "false",
    }
    try:
        for _ in range(100):
            try:
                response = await exchange.private_post_openorders(params)
            except Exception as exc:
                raise ValueError(f"Kraken open-order request failed ({type(exc).__name__})") from None
            if not isinstance(response, dict) or response.get("error") != []:
                raise ValueError("Kraken rejected the open-order request")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("open"), dict):
                raise ValueError("Kraken returned an invalid open-order response")
            page = result["open"]
            previous_count = len(orders)
            for order_id, order in page.items():
                if not isinstance(order_id, str) or not order_id.strip():
                    raise ValueError("Kraken returned an invalid order ID")
                _check_open_order(order)
                if order_id in orders and orders[order_id] != order:
                    raise ValueError("An open order changed between pages; read again")
                orders[order_id] = order
            cursor = result.get("cursor", {})
            if not isinstance(cursor, dict):
                raise ValueError("Kraken returned an invalid order cursor")
            next_cursor = cursor.get("next")
            if next_cursor is None or next_cursor == "":
                return orders
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise ValueError("Kraken returned an invalid order cursor")
            if next_cursor in cursors or len(orders) == previous_count:
                raise ValueError("Kraken open-order pagination did not advance")
            cursors.add(next_cursor)
            params = {**params, "cursor": next_cursor}
        raise ValueError("Kraken open-order pagination exceeded 100 pages")
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken open-order client") from None
