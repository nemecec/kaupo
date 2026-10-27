"""Resolve fill ledger references without assuming fee currency or asset aliases."""

from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt.async_support as ccxt


def fill_ledger_ids(fills: dict[str, Any]) -> list[str]:
    ids: dict[str, None] = {}
    for fill in fills.values():
        references = fill.get("ledgers") if isinstance(fill, dict) else None
        if not isinstance(references, list) or not references:
            raise ValueError("Every fill must have ledger references for ledger inspection")
        for ledger_id in references:
            if (
                not isinstance(ledger_id, str)
                or not ledger_id
                or "," in ledger_id
                or any(char.isspace() for char in ledger_id)
            ):
                raise ValueError("A fill has an invalid ledger reference")
            ids[ledger_id] = None
            if len(ids) > 2000:
                raise ValueError("Ledger inspection exceeds 2000 IDs; use a smaller fill window")
    return list(ids)


def _check_ledger(entry: object, *, require_refid: bool = True) -> None:
    if not isinstance(entry, dict):
        raise ValueError("Kraken returned an invalid ledger entry")
    fields = ("refid", "type", "aclass", "asset") if require_refid else ("type", "aclass", "asset")
    for field in fields:
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ValueError("Kraken returned an invalid ledger identifier or description")
    for field in ("amount", "fee", "balance", "time"):
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError("Kraken returned an invalid ledger amount or timestamp")
        if field != "time" and not isinstance(value, str):
            raise ValueError("Kraken returned a non-string ledger amount")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("Kraken returned an invalid ledger amount or timestamp") from None
        if not number.is_finite() or (field == "time" and number < 0):
            raise ValueError("Kraken returned an invalid ledger amount or timestamp")


async def read_fill_ledgers(
    api_key: str, api_secret: str, *, units: str, fills: dict[str, Any]
) -> dict[str, Any]:
    """Require every referenced entry; never return a successful partial collection."""
    if units not in ("base", "rebased"):
        raise ValueError("Ledger units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    ids = fill_ledger_ids(fills)
    if not ids:
        return {}
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    entries: dict[str, Any] = {}
    try:
        for offset in range(0, len(ids), 20):
            batch = ids[offset : offset + 20]
            try:
                response = await exchange.private_post_queryledgers(
                    {"id": ",".join(batch), "rebase_multiplier": units}
                )
            except Exception as exc:
                raise ValueError(f"Kraken ledger request failed ({type(exc).__name__})") from None
            if not isinstance(response, dict) or response.get("error") != []:
                raise ValueError("Kraken rejected the ledger request")
            result = response.get("result")
            if not isinstance(result, dict) or set(result) != set(batch):
                raise ValueError("Kraken returned missing or unexpected ledger IDs")
            for entry in result.values():
                _check_ledger(entry)
            entries.update(result)
        return entries
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken ledger client") from None
