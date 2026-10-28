"""Read exact Kraken order IDs, including terminal states, with explicit units."""

from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt.async_support as ccxt

from kaupo.venues.xstocks_account import _check_order


def check_order_request(order_ids: list[str], units: str) -> None:
    if units not in ("base", "rebased"):
        raise ValueError("Order units must be rebased or base")
    if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= 50:
        raise ValueError("Specify between 1 and 50 exact Kraken order IDs")
    if any(
        not isinstance(value, str) or not value or "," in value or any(c.isspace() for c in value)
        for value in order_ids
    ):
        raise ValueError("Invalid Kraken order ID")
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("Duplicate Kraken order IDs")


def _check_status(order: object) -> None:
    _check_order(order, statuses=("pending", "open", "closed", "canceled", "expired"))
    assert isinstance(order, dict)
    terminal = order["status"] in ("closed", "canceled", "expired")
    times = {}
    for field in ("opentm", "closetm") if terminal else ("opentm",):
        value = order.get(field)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError("Kraken returned an invalid order timestamp")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("Kraken returned an invalid order timestamp") from None
        if not number.is_finite() or number < 0:
            raise ValueError("Kraken returned an invalid order timestamp")
        times[field] = number
    if terminal and times["closetm"] < times["opentm"]:
        raise ValueError("Kraken returned inconsistent order timestamps")
    trades = order.get("trades", [])
    if not isinstance(trades, list) or any(not isinstance(t, str) or not t.strip() for t in trades):
        raise ValueError("Kraken returned invalid order trade references")
    if len(set(trades)) != len(trades):
        raise ValueError("Kraken returned duplicate order trade references")


async def read_order_status(
    api_key: str, api_secret: str, *, units: str, order_ids: list[str]
) -> dict[str, Any]:
    check_order_request(order_ids, units)
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    try:
        try:
            response = await exchange.private_post_queryorders(
                {
                    "txid": ",".join(order_ids),
                    "rebase_multiplier": units,
                    "trades": "true",
                    "consolidate_taker": "false",
                }
            )
        except Exception as exc:
            raise ValueError(f"Kraken order-status request failed ({type(exc).__name__})") from None
        if not isinstance(response, dict) or response.get("error") != []:
            raise ValueError("Kraken rejected the order-status request")
        result = response.get("result")
        if not isinstance(result, dict) or set(result) != set(order_ids):
            raise ValueError("Kraken did not return exactly the requested order IDs")
        for order in result.values():
            _check_status(order)
        return result
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken order-status client") from None
