from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.data.xstocks import XStocksClient
from kaupo.venues import xstocks_fills, xstocks_ledgers
from kaupo.venues.xstocks_ledger_check import check_fill_ledgers

FILL = {
    "pair": "AAPLxUSD",
    "type": "buy",
    "margin": "0",
    "vol": "1.25",
    "cost": "125.00",
    "fee": "999",
    "ledgers": ["base", "quote"],
}
LEDGERS = {
    "base": {"refid": "trade", "type": "trade", "asset": "AAPLx", "amount": "1.25", "fee": "0"},
    "quote": {"refid": "trade", "type": "trade", "asset": "ZUSD", "amount": "-125", "fee": "-0.025"},
}
IDENTITIES = {"fill_pairs": {"trade": "AAPLX/USD"}, "ledger_assets": {"base": "AAPLX", "quote": "USD"}}


def check(fill=None, ledgers=None, identities=None):
    return check_fill_ledgers(
        {"trade": FILL if fill is None else fill},
        LEDGERS if ledgers is None else ledgers,
        IDENTITIES if identities is None else identities,
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_principal_and_actual_fees_are_separate(side):
    fill, entries = deepcopy(FILL), deepcopy(LEDGERS)
    fill["type"] = side
    if side == "sell":
        entries["base"]["amount"] = "-1.25"
        entries["quote"]["amount"] = "125"
    entries["base"]["fee"] = "0.001"
    before = deepcopy((fill, entries))
    report = check(fill, entries)
    assert report["status"] == "amounts_match"
    row = report["fills"]["trade"]
    assert row["fees_by_asset"] == {"AAPLX": "0.001", "USD": "-0.025"}
    assert row["net_movements"] == (
        {"AAPLX": "1.249", "USD": "-124.975"} if side == "buy" else {"AAPLX": "-1.251", "USD": "125.025"}
    )
    assert (fill, entries) == before


def test_split_entries_sum_exactly_and_do_not_use_price_or_fee_estimate():
    fill = {**FILL, "ledgers": ["base", "quote", "other"]}
    entries = deepcopy(LEDGERS)
    entries["base"]["amount"] = "0.00000000000000000001"
    entries["other"] = {**entries["base"], "amount": "1.24999999999999999999"}
    identities = deepcopy(IDENTITIES)
    identities["ledger_assets"]["other"] = "AAPLX"
    assert check(fill, entries, identities)["status"] == "amounts_match"


def test_mismatch_is_reported_without_tolerance():
    report = check({**FILL, "vol": "1.25000000000000000001"})
    assert report["status"] == "needs_review"
    assert report["counts"] == {"amounts_mismatch": 1}
    assert report["fills"]["trade"]["ledger_amounts"]["AAPLX"] == "1.25"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("margin", "1", "unsupported_margin_fill"),
        ("margin", None, "missing_or_invalid_decimal"),
        ("type", "bad", "invalid_side"),
        ("vol", "0", "invalid_volume_or_cost"),
        ("cost", "-1", "invalid_volume_or_cost"),
        ("cost", "bad", "missing_or_invalid_decimal"),
        ("vol", "NaN", "non_finite_decimal"),
        ("vol", "1e100", "unsupported_decimal_precision"),
        ("vol", "1" * 65, "unsupported_decimal_precision"),
        ("ledgers", [], "missing_ledger_references"),
        ("ledgers", "base", "missing_ledger_references"),
        ("ledgers", [None], "invalid_ledger_reference"),
        ("ledgers", ["base", "base"], "duplicate_ledger_reference"),
        ("ledgers", ["base"], "missing_base_or_quote_entry"),
        ("ledgers", ["missing"], "missing_ledger_entry"),
    ],
)
def test_unsupported_fill_is_unresolved(field, value, reason):
    report = check({**FILL, field: value})
    assert report["status"] == "needs_review"
    assert report["fills"]["trade"] == {"status": "unresolved", "reason": reason}


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("refid", "another-trade", "ledger_reference_mismatch"),
        ("type", "margin", "unsupported_ledger_type"),
        ("subtype", "adjustment", "unsupported_ledger_type"),
        ("fee", "Infinity", "non_finite_decimal"),
        ("amount", None, "missing_or_invalid_decimal"),
    ],
)
def test_unsupported_ledger_is_unresolved(field, value, reason):
    entries = deepcopy(LEDGERS)
    entries["base"][field] = value
    assert check(ledgers=entries)["fills"]["trade"]["reason"] == reason


@pytest.mark.parametrize(
    "identities,reason",
    [
        ({"fill_pairs": {}, "ledger_assets": {}}, "unmapped_pair"),
        ({**IDENTITIES, "ledger_assets": {}}, "unmapped_or_third_asset"),
        ({**IDENTITIES, "ledger_assets": {"base": "KFEE", "quote": "USD"}}, "unmapped_or_third_asset"),
    ],
)
def test_unknown_or_third_assets_do_not_match(identities, reason):
    assert check(identities=identities)["fills"]["trade"]["reason"] == reason


def test_shared_ledgers_do_not_double_count():
    identities = {**IDENTITIES, "fill_pairs": {"trade": "AAPLX/USD", "other": "AAPLX/USD"}}
    report = check_fill_ledgers({"trade": FILL, "other": FILL}, LEDGERS, identities)
    assert report["counts"] == {"unresolved": 2}
    assert all(row["reason"] == "shared_ledger_reference" for row in report["fills"].values())


def test_empty_collection_is_not_a_match():
    assert check_fill_ledgers({}, {}, IDENTITIES) == {"status": "no_fills", "counts": {}, "fills": {}}


@pytest.mark.parametrize(
    "volume,expected_exit,status",
    [("1.25", 0, "amounts_match"), ("1.2", 1, "needs_review"), ("bad", 1, "needs_review")],
)
def test_cli_check_implies_reads_and_mapping_and_sets_exit(monkeypatch, volume, expected_exit, status):
    import json

    read_ledgers = AsyncMock(return_value=LEDGERS)
    monkeypatch.setattr(
        xstocks_fills, "read_fills", AsyncMock(return_value={"trade": {**FILL, "vol": volume}})
    )
    monkeypatch.setattr(xstocks_ledgers, "read_fill_ledgers", read_ledgers)
    monkeypatch.setattr(
        XStocksClient,
        "identity_metadata",
        AsyncMock(
            return_value={
                "AAPLSPVUSD": {
                    "aclass_base": "tokenized_asset",
                    "altname": "AAPLxUSD",
                    "wsname": "AAPLx/USD",
                    "base": "AAPLx",
                    "quote": "ZUSD",
                }
            }
        ),
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(
        cli.app,
        ["run", "xstocks-fills", "--units", "base", "--start", "100", "--end", "200", "--check-ledgers"],
    )
    assert result.exit_code == expected_exit, result.output
    report = json.loads(result.stdout)
    assert report["ledger_check"]["status"] == status
    assert "ledgers" in report and "identities" in report
    read_ledgers.assert_awaited_once()
