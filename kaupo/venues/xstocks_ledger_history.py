"""Bounded ledger history for an explicit asset scope, including non-trade activity."""

from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from kaupo.venues.xstocks_fills import check_fill_window
from kaupo.venues.xstocks_ledgers import _check_ledger


def check_ledger_request(asset: str, asset_class: str, units: str, start: int, end: int) -> None:
    if (
        not isinstance(asset, str)
        or not asset
        or asset.lower() == "all"
        or "," in asset
        or any(c.isspace() for c in asset)
    ):
        raise ValueError("Specify one exact Kraken asset ID")
    if asset_class not in ("currency", "tokenized_asset"):
        raise ValueError("Asset class must be currency or tokenized_asset")
    if units not in ("base", "rebased"):
        raise ValueError("Ledger units must be rebased or base")
    try:
        check_fill_window(start, end)
    except ValueError as exc:
        raise ValueError(str(exc).replace("Fill", "Ledger")) from None


async def read_ledger_history(
    api_key: str, api_secret: str, *, units: str, start: int, end: int, asset: str, asset_class: str
) -> dict[str, Any]:
    """Read all cursor pages; never expose a partial collection as successful evidence."""
    check_ledger_request(asset, asset_class, units, start, end)
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    entries: dict[str, Any] = {}
    cursors: set[str] = set()
    params: dict[str, Any] = {
        "rebase_multiplier": units,
        "with_cursor": "true",
        "asset": asset,
        "aclass": asset_class,
        "type": "all",
        "start": start,
        "end": end,
    }
    try:
        for _ in range(100):
            try:
                response = await exchange.private_post_ledgers(params)
            except Exception as exc:
                raise ValueError(f"Kraken ledger history request failed ({type(exc).__name__})") from None
            if not isinstance(response, dict) or response.get("error") != []:
                raise ValueError("Kraken rejected the ledger history request")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("ledger"), dict):
                raise ValueError("Kraken returned an invalid entry response")
            page = result["ledger"]
            previous_count = len(entries)
            for ledger_id, entry in page.items():
                if not isinstance(ledger_id, str) or not ledger_id.strip():
                    raise ValueError("Kraken returned an invalid entry ID")
                _check_ledger(entry, require_refid=False)
                if entry["asset"] != asset or entry["aclass"] != asset_class:
                    raise ValueError("Kraken returned a ledger outside the requested asset scope")
                if not start < Decimal(str(entry["time"])) <= end:
                    raise ValueError("Kraken returned a ledger outside the requested window")
                if ledger_id in entries and entries[ledger_id] != entry:
                    raise ValueError("A ledger entry changed between pages; read again")
                entries[ledger_id] = entry
            cursor = result.get("cursor", {})
            if not isinstance(cursor, dict):
                raise ValueError("Kraken returned an invalid entry cursor")
            next_cursor = cursor.get("next")
            if next_cursor is None or next_cursor == "":
                return entries
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise ValueError("Kraken returned an invalid entry cursor")
            if next_cursor in cursors or len(entries) == previous_count:
                raise ValueError("Kraken ledger history pagination did not advance")
            cursors.add(next_cursor)
            params = {**params, "cursor": next_cursor}
        raise ValueError("Kraken ledger history pagination exceeded 100 pages")
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken ledger history client") from None
