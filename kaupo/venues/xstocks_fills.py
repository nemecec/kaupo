"""Read-only, bounded Kraken fill history for account inspection."""

from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt.async_support as ccxt


def check_fill_window(start: int, end: int) -> None:
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= 253402300799:
        raise ValueError("Fill bounds must be Unix seconds with 0 <= start < end")
    if end - start > 31 * 86400:
        raise ValueError("Fill history window must not exceed 31 days")


def _check_fill(fill: object, *, start: int, end: int) -> None:
    if not isinstance(fill, dict):
        raise ValueError("Kraken returned an invalid fill")
    for field in ("ordertxid", "pair", "ordertype"):
        if not isinstance(fill.get(field), str) or not fill[field].strip():
            raise ValueError("Kraken returned an invalid fill identifier or description")
    if fill.get("type") not in ("buy", "sell"):
        raise ValueError("Kraken returned an invalid fill side")
    for field in ("price", "vol", "cost", "fee", "time"):
        value = fill.get(field)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError("Kraken returned an invalid fill amount or timestamp")
        if field != "time" and not isinstance(value, str):
            raise ValueError("Kraken returned a non-string fill amount")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("Kraken returned an invalid fill amount or timestamp") from None
        if not amount.is_finite():
            raise ValueError("Kraken returned a non-finite fill amount or timestamp")
        if (field in ("price", "vol") and amount <= 0) or (field == "cost" and amount < 0):
            raise ValueError("Kraken returned an invalid fill amount")
        if field == "time" and not start < amount <= end:
            raise ValueError("Kraken returned a fill outside the requested window")


async def read_fills(api_key: str, api_secret: str, *, units: str, start: int, end: int) -> dict[str, Any]:
    """Read all cursor pages; never expose a partial collection as successful evidence."""
    check_fill_window(start, end)
    if units not in ("rebased", "base"):
        raise ValueError("Fill units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    fills: dict[str, Any] = {}
    cursors: set[str] = set()
    params: dict[str, Any] = {
        "rebase_multiplier": units,
        "with_cursor": "true",
        "limit": 100,
        "trades": "false",
        "consolidate_taker": "false",
        "ledgers": "true",
        "type": "all",
        "start": start,
        "end": end,
    }
    try:
        for _ in range(100):
            try:
                response = await exchange.private_post_tradeshistory(params)
            except Exception as exc:
                raise ValueError(f"Kraken fill request failed ({type(exc).__name__})") from None
            if not isinstance(response, dict) or response.get("error") != []:
                raise ValueError("Kraken rejected the fill request")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("trades"), dict):
                raise ValueError("Kraken returned an invalid fill response")
            page = result["trades"]
            previous_count = len(fills)
            for trade_id, fill in page.items():
                if not isinstance(trade_id, str) or not trade_id.strip():
                    raise ValueError("Kraken returned an invalid fill ID")
                _check_fill(fill, start=start, end=end)
                if trade_id in fills and fills[trade_id] != fill:
                    raise ValueError("A fill changed between pages; read again")
                fills[trade_id] = fill
            cursor = result.get("cursor", {})
            if not isinstance(cursor, dict):
                raise ValueError("Kraken returned an invalid fill cursor")
            next_cursor = cursor.get("next")
            if next_cursor is None or next_cursor == "":
                return fills
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise ValueError("Kraken returned an invalid fill cursor")
            if next_cursor in cursors or len(fills) == previous_count:
                raise ValueError("Kraken fill pagination did not advance")
            cursors.add(next_cursor)
            params = {**params, "cursor": next_cursor}
        raise ValueError("Kraken fill pagination exceeded 100 pages")
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken fill client") from None
