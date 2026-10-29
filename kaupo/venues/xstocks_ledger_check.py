"""Read-only principal checks and asset-specific fee evidence for spot fills."""

from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from kaupo.domain import Pair


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("missing_or_invalid_decimal")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("missing_or_invalid_decimal") from None
    if not number.is_finite():
        raise ValueError("non_finite_decimal")
    # Bound inputs so all sums and subtractions fit the local precision exactly.
    parts = number.as_tuple()
    if len(parts.digits) > 64 or not isinstance(parts.exponent, int) or not -30 <= parts.exponent <= 30:
        raise ValueError("unsupported_decimal_precision")
    return number


def _check_fill(
    trade_id: str,
    fill: dict[str, Any],
    ledgers: dict[str, Any],
    identities: dict[str, Any],
    owners: Counter[str],
) -> dict[str, Any]:
    symbol = identities["fill_pairs"].get(trade_id)
    if symbol is None:
        raise ValueError("unmapped_pair")
    pair = Pair.parse(symbol)
    if _decimal(fill.get("margin")) != 0:
        raise ValueError("unsupported_margin_fill")
    if fill.get("type") not in ("buy", "sell"):
        raise ValueError("invalid_side")
    volume, cost = _decimal(fill.get("vol")), _decimal(fill.get("cost"))
    if volume <= 0 or cost < 0:
        raise ValueError("invalid_volume_or_cost")
    references = fill.get("ledgers")
    if not isinstance(references, list) or not references:
        raise ValueError("missing_ledger_references")
    if any(not isinstance(ref, str) or not ref for ref in references):
        raise ValueError("invalid_ledger_reference")
    if len(set(references)) != len(references):
        raise ValueError("duplicate_ledger_reference")
    if any(owners[ref] > 1 for ref in references):
        raise ValueError("shared_ledger_reference")
    amounts = {pair.base: Decimal(0), pair.quote: Decimal(0)}
    fees = {pair.base: Decimal(0), pair.quote: Decimal(0)}
    other_fees: dict[str, Decimal] = {}
    other_fee_classes: dict[str, str] = {}
    seen: set[str] = set()
    for ref in references:
        entry = ledgers.get(ref)
        if not isinstance(entry, dict):
            raise ValueError("missing_ledger_entry")
        if entry.get("refid") != trade_id:
            raise ValueError("ledger_reference_mismatch")
        if entry.get("type") != "trade" or entry.get("subtype", "") != "":
            raise ValueError("unsupported_ledger_type")
        if ref in identities.get("ledger_class_mismatches", {}):
            raise ValueError("ledger_asset_class_mismatch")
        asset = identities["ledger_assets"].get(ref)
        amount, fee = _decimal(entry.get("amount")), _decimal(entry.get("fee"))
        if asset not in amounts:
            if amount != 0:
                raise ValueError("unmapped_or_third_asset")
            asset_id = entry.get("asset")
            if not isinstance(asset_id, str) or not asset_id or any(c.isspace() for c in asset_id):
                raise ValueError("invalid_fee_asset_id")
            asset_class = entry.get("aclass")
            if not isinstance(asset_class, str) or not asset_class or any(c.isspace() for c in asset_class):
                raise ValueError("invalid_fee_asset_class")
            if asset_id in other_fee_classes and other_fee_classes[asset_id] != asset_class:
                raise ValueError("conflicting_fee_asset_classes")
            other_fee_classes[asset_id] = asset_class
            other_fees[asset_id] = other_fees.get(asset_id, Decimal(0)) + fee
            continue
        seen.add(asset)
        amounts[asset] += amount
        fees[asset] += fee
    if seen != set(amounts):
        raise ValueError("missing_base_or_quote_entry")
    sign = Decimal(1) if fill["type"] == "buy" else Decimal(-1)
    expected = {pair.base: sign * volume, pair.quote: -sign * cost}
    return {
        "status": "amounts_match" if amounts == expected else "amounts_mismatch",
        "pair": symbol,
        "expected_amounts": {asset: str(value) for asset, value in expected.items()},
        "ledger_amounts": {asset: str(value) for asset, value in amounts.items()},
        "fees_by_asset": {asset: str(value) for asset, value in fees.items()},
        "net_movements": {asset: str(amounts[asset] - fees[asset]) for asset in amounts},
        "other_fee_asset_classes": dict(sorted(other_fee_classes.items())),
        "other_fees_by_asset_id": {asset: str(fee) for asset, fee in sorted(other_fees.items())},
        "other_net_movements_by_asset_id": {asset: str(-fee) for asset, fee in sorted(other_fees.items())},
    }


def check_fill_ledgers(
    fills: dict[str, Any], ledgers: dict[str, Any], identities: dict[str, Any]
) -> dict[str, Any]:
    """Report exact principal agreement; never interpret this as account readiness."""
    owners: Counter[str] = Counter()
    for fill in fills.values():
        references = fill.get("ledgers")
        if isinstance(references, list):
            owners.update(set(ref for ref in references if isinstance(ref, str)))
    results: dict[str, Any] = {}
    with localcontext() as context:
        context.prec = 128
        for trade_id, fill in fills.items():
            try:
                results[trade_id] = _check_fill(trade_id, fill, ledgers, identities, owners)
            except ValueError as exc:
                results[trade_id] = {"status": "unresolved", "reason": str(exc)}
    counts = Counter(result["status"] for result in results.values())
    status = "no_fills" if not fills else "amounts_match"
    if counts["amounts_mismatch"] or counts["unresolved"]:
        status = "needs_review"
    return {"status": status, "counts": dict(counts), "fills": results}
