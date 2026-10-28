import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_ledger_history as history
from kaupo.venues.xstocks_balance_links import check_window_balances

ROWS = {
    "first": {
        "asset": "AAPLx",
        "aclass": "tokenized_asset",
        "time": 101,
        "amount": "2",
        "fee": "0.1",
        "balance": "11.9",
    },
    "last": {
        "asset": "AAPLx",
        "aclass": "tokenized_asset",
        "time": 200,
        "amount": "-1",
        "fee": "-0.2",
        "balance": "11.1",
    },
}


def check(rows, opening="10", closing="11.1"):
    return check_window_balances(
        rows, asset="AAPLx", asset_class="tokenized_asset", opening=opening, closing=closing
    )


def test_full_window_checks_boundaries_and_internal_links_without_mutation():
    rows = dict(reversed(list(deepcopy(ROWS).items())))
    before = deepcopy(rows)
    report = check(rows)
    assert report["status"] == "balances_match"
    assert report["anchor_source"] == "operator"
    assert [c["ledger_id"] for c in report["boundaries"]] == ["first", "last"]
    assert all(c["status"] == "match" for c in report["boundaries"] + report["links"])
    assert rows == before


@pytest.mark.parametrize(
    "opening,closing,boundary,difference", [("9", "11.1", "opening", "1.0"), ("10", "12.1", "closing", "1.0")]
)
def test_boundary_mismatch(opening, closing, boundary, difference):
    report = check(ROWS, opening, closing)
    assert report["status"] == "needs_review"
    mismatch = next(c for c in report["boundaries"] if c["boundary"] == boundary)
    assert mismatch["difference"] == difference
    assert mismatch["status"] == "gap"


def test_internal_gap_cannot_be_hidden_by_matching_boundaries():
    rows = deepcopy(ROWS)
    rows["last"]["amount"] = "-2"
    report = check(rows)
    assert all(c["status"] == "match" for c in report["boundaries"])
    assert report["links"][0]["status"] == "gap"
    assert report["status"] == "needs_review"


@pytest.mark.parametrize("closing,status", [("10.00", "balances_match"), ("11", "needs_review")])
def test_empty_window_compares_supplied_balances(closing, status):
    report = check({}, closing=closing)
    assert report["status"] == status
    assert report["boundaries"][0]["boundary"] == "empty_window"
    assert report["links"] == []


def test_single_entry_checks_both_boundaries():
    assert check({"first": ROWS["first"]}, closing="11.9")["status"] == "balances_match"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("asset", "other", "asset_scope_mismatch"),
        ("aclass", "currency", "asset_scope_mismatch"),
        ("time", 101, "ambiguous_timestamp_order"),
        ("fee", "NaN", "non_finite_decimal"),
        ("balance", "1e100", "unsupported_decimal_precision"),
    ],
)
def test_unresolved_data_does_not_claim_match(field, value, reason):
    rows = deepcopy(ROWS)
    rows["last"][field] = value
    report = check(rows)
    assert report["status"] == "needs_review"
    assert report["reason"] == reason
    assert "boundaries" not in report


def test_exact_precision_and_negative_balances():
    rows = {
        "first": {
            **ROWS["first"],
            "amount": "0.00000000000000000001",
            "fee": "0",
            "balance": "-0.99999999999999999999",
        }
    }
    assert check(rows, "-1", "-0.99999999999999999999")["status"] == "balances_match"


@pytest.fixture
def reader(monkeypatch):
    read = AsyncMock(return_value=ROWS)
    monkeypatch.setattr(history, "read_ledger_history", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return read


def invoke(extra):
    return CliRunner().invoke(
        cli.app,
        [
            "run",
            "xstocks-ledgers",
            "--asset",
            "AAPLx",
            "--asset-class",
            "tokenized_asset",
            "--units",
            "base",
            "--start",
            "100",
            "--end",
            "200",
            *extra,
        ],
    )


@pytest.mark.parametrize("closing,code", [("11.1", 0), ("12", 1)])
def test_cli_boundary_report_without_separate_links_flag(reader, closing, code):
    result = invoke(["--opening-balance", "10", "--closing-balance", closing])
    assert result.exit_code == code, result.output
    body = json.loads(result.stdout)
    assert body["balance_window"]["opening_balance"] == "10"
    assert body["balance_window"]["closing_balance"] == closing
    assert body["rebase_multiplier"] == "base"
    assert body["ledgers"] == ROWS
    assert "balance_links" not in body
    reader.assert_awaited_once()


@pytest.mark.parametrize(
    "extra",
    [
        ["--opening-balance", "10"],
        ["--closing-balance", "10"],
        ["--opening-balance", "NaN", "--closing-balance", "1"],
        ["--opening-balance", "1", "--closing-balance", "1e100"],
    ],
)
def test_invalid_cli_anchors_fail_before_network(reader, extra):
    result = invoke(extra)
    assert result.exit_code == 2
    reader.assert_not_awaited()
    assert "ledger_history_read" not in result.output


def test_legacy_flag_still_reports_insufficient_single_entry(reader):
    reader.return_value = {"first": ROWS["first"]}
    result = invoke(["--opening-balance", "10", "--closing-balance", "11.9", "--check-balance-links"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["balance_window"]["status"] == "balances_match"
    assert body["balance_links"]["status"] == "needs_review"
