from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_fills, xstocks_ledgers
from kaupo.venues.xstocks_balance_links import check_balance_links

ROWS = {
    "first": {
        "aclass": "currency",
        "asset": "ZUSD",
        "time": 100,
        "amount": "10",
        "fee": "1",
        "balance": "100",
    },
    "second": {
        "aclass": "currency",
        "asset": "ZUSD",
        "time": 200,
        "amount": "-20",
        "fee": "-0.5",
        "balance": "80.5",
    },
}


def test_chronological_links_with_rebates_ignore_input_order():
    rows = dict(reversed(list(ROWS.items())))
    before = deepcopy(rows)
    report = check_balance_links(rows)
    assert report["status"] == "links_match"
    assert report["scope"] == "selected_ledgers"
    asset = report["assets"][0]
    assert asset["anchor_id"] == "first"
    assert asset["last_id"] == "second"
    assert asset["links"] == [
        {
            "previous_id": "first",
            "current_id": "second",
            "expected_balance": "80.5",
            "actual_balance": "80.5",
            "difference": "0.0",
            "status": "match",
        }
    ]
    assert rows == before


def test_gap_is_reported_with_signed_difference():
    rows = deepcopy(ROWS)
    rows["second"]["balance"] = "90.5"
    report = check_balance_links(rows)
    assert report["status"] == "needs_review"
    assert report["counts"] == {"gaps_detected": 1}
    assert report["assets"][0]["links"][0]["difference"] == "10.0"


def test_tied_timestamps_never_guess_from_ids_or_balances():
    rows = deepcopy(ROWS)
    rows["second"]["time"] = 100
    report = check_balance_links(rows)
    assert report["assets"][0]["status"] == "unresolved"
    assert report["assets"][0]["reason"] == "ambiguous_timestamp_order"
    assert "links" not in report["assets"][0]


@pytest.mark.parametrize(
    "field,value", [("asset", "USD"), ("asset", "ZUSD.F"), ("aclass", "tokenized_asset")]
)
def test_exact_identity_groups_do_not_mix(field, value):
    rows = deepcopy(ROWS)
    rows["second"][field] = value
    report = check_balance_links(rows)
    assert report["status"] == "needs_review"
    assert report["counts"] == {"insufficient_entries": 2}
    assert len(report["assets"]) == 2


def test_empty_and_single_entry_do_not_claim_continuity():
    assert check_balance_links({}) == {
        "status": "no_ledgers",
        "scope": "selected_ledgers",
        "counts": {},
        "assets": [],
    }
    report = check_balance_links({"first": ROWS["first"]})
    assert report["status"] == "needs_review"
    assert report["assets"][0]["status"] == "insufficient_entries"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("time", True, "invalid_timestamp"),
        ("time", None, "invalid_timestamp"),
        ("time", -1, "invalid_timestamp"),
        ("time", float("inf"), "non_finite_decimal"),
        ("amount", "bad", "missing_or_invalid_decimal"),
        ("fee", "NaN", "non_finite_decimal"),
        ("balance", "1e100", "unsupported_decimal_precision"),
    ],
)
def test_bad_numeric_data_is_unresolved(field, value, reason):
    rows = deepcopy(ROWS)
    rows["second"][field] = value
    report = check_balance_links(rows)
    assert report["status"] == "needs_review"
    assert report["assets"][0]["reason"] == reason


@pytest.mark.parametrize("entry", [None, {}, {"asset": "", "aclass": "currency"}])
def test_invalid_asset_identity_fails(entry):
    with pytest.raises(ValueError, match="Invalid ledger asset identity"):
        check_balance_links({"id": entry})


def test_exact_arithmetic_and_positive_fee():
    rows = deepcopy(ROWS)
    rows["first"]["balance"] = "1.00000000000000000001"
    rows["second"].update(amount="-0.00000000000000000001", fee="0.1", balance="0.9")
    assert check_balance_links(rows)["status"] == "links_match"


@pytest.mark.parametrize(
    "balance,expected_status,exit_code", [("80.5", "links_match", 0), ("90", "needs_review", 1)]
)
def test_cli_includes_ledgers_without_requiring_mapping(monkeypatch, balance, expected_status, exit_code):
    import json

    rows = deepcopy(ROWS)
    rows["second"]["balance"] = balance
    monkeypatch.setattr(xstocks_fills, "read_fills", AsyncMock(return_value={"f": {"ledgers": list(rows)}}))
    ledger_read = AsyncMock(return_value=rows)
    monkeypatch.setattr(xstocks_ledgers, "read_fill_ledgers", ledger_read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(
        cli.app,
        ["run", "xstocks-fills", "--units", "base", "--start", "50", "--end", "250", "--check-balance-links"],
    )
    assert result.exit_code == exit_code, result.output
    body = json.loads(result.stdout)
    assert body["balance_links"]["status"] == expected_status
    assert body["ledgers"] == rows
    assert "identities" not in body and "ledger_check" not in body
    ledger_read.assert_awaited_once()
