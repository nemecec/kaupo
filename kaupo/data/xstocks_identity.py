"""Exact mappings from current public metadata, without inferred asset aliases."""

from typing import Any

from kaupo.domain import Pair


def identity_maps(rows: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    pairs: dict[str, str] = {}
    assets: dict[str, str] = {}
    classes: dict[str, str] = {}

    def add(target: dict[str, str], alias: str, canonical: str) -> None:
        if alias in target and target[alias] != canonical:
            raise ValueError("Conflicting Kraken identity metadata")
        target[alias] = canonical

    for key, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError("Invalid Kraken identity metadata")
        if (
            row.get("aclass_base") != "tokenized_asset"
            or row.get("execution_venue", "international") != "international"
        ):
            continue
        values = [key, *(row.get(field) for field in ("altname", "wsname", "base", "quote", "aclass_quote"))]
        if any(
            not isinstance(value, str) or not value or any(c.isspace() for c in value) for value in values
        ):
            raise ValueError("Missing Kraken pair or asset identity")
        pair = Pair.parse(row["wsname"])
        if not pair.base or not pair.quote or pair.base == pair.quote:
            raise ValueError("Invalid Kraken pair identity")
        for alias in (key, row["altname"], row["wsname"]):
            add(pairs, alias, str(pair))
        add(assets, row["base"], pair.base)
        add(assets, row["quote"], pair.quote)
        add(classes, row["base"], row["aclass_base"])
        add(classes, row["quote"], row["aclass_quote"])
    return pairs, assets, classes


def map_account_identities(
    rows: dict[str, Any], fills: dict[str, Any], ledgers: dict[str, Any] | None
) -> dict[str, Any]:
    """Annotate exact matches separately from the original private response."""
    pairs, assets, classes = identity_maps(rows)
    fill_pairs: dict[str, str] = {}
    ledger_assets: dict[str, str] = {}
    class_mismatches: dict[str, Any] = {}
    unknown_pairs: set[str] = set()
    unknown_assets: set[str] = set()
    for trade_id, fill in fills.items():
        symbol = fill["pair"]
        if symbol in pairs:
            fill_pairs[trade_id] = pairs[symbol]
        else:
            unknown_pairs.add(symbol)
    for ledger_id, entry in (ledgers or {}).items():
        asset = entry["asset"]
        if asset in assets and entry.get("aclass") == classes[asset]:
            ledger_assets[ledger_id] = assets[asset]
        else:
            unknown_assets.add(asset)
            if asset in assets:
                class_mismatches[ledger_id] = {
                    "asset": asset,
                    "expected_class": classes[asset],
                    "actual_class": entry.get("aclass"),
                }
    return {
        "fill_pairs": fill_pairs,
        "ledger_assets": ledger_assets,
        "unmapped_pairs": sorted(unknown_pairs),
        "unmapped_assets": sorted(unknown_assets),
        "ledger_class_mismatches": class_mismatches,
    }
