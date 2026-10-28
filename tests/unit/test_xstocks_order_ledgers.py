import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.data.xstocks import XStocksClient
from kaupo.venues import xstocks_ledgers, xstocks_order_fills, xstocks_order_status

ORDER = {
    "status": "expired",
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit"},
    "vol": "2",
    "vol_exec": "0.5",
    "expiretm": 200,
    "opentm": 100,
    "closetm": 200,
    "trades": ["t1"],
}
FILL = {
    "ordertxid": "o1",
    "pair": "AAPLSPVUSD",
    "type": "buy",
    "ordertype": "limit",
    "time": 150,
    "vol": "0.5",
    "price": "100",
    "cost": "50",
    "fee": "999",
    "margin": "0",
    "ledgers": ["base", "quote", "fee"],
}
LEDGERS = {
    "base": {"refid": "t1", "type": "trade", "asset": "AAPLx", "amount": "0.5", "fee": "0"},
    "quote": {"refid": "t1", "type": "trade", "asset": "ZUSD", "amount": "-50", "fee": "-0.1"},
    "fee": {"refid": "t1", "type": "trade", "asset": "KFEE", "amount": "0", "fee": "0.2"},
}
METADATA = {
    "AAPLSPVUSD": {
        "aclass_base": "tokenized_asset",
        "altname": "AAPLxUSD",
        "wsname": "AAPLx/USD",
        "base": "AAPLx",
        "quote": "ZUSD",
    }
}


@pytest.fixture
def readers(monkeypatch):
    order = AsyncMock(return_value={"o1": deepcopy(ORDER)})
    fill = AsyncMock(return_value={"t1": deepcopy(FILL)})
    ledger = AsyncMock(return_value=deepcopy(LEDGERS))
    metadata = AsyncMock(return_value=deepcopy(METADATA))
    monkeypatch.setattr(xstocks_order_status, "read_order_status", order)
    monkeypatch.setattr(xstocks_order_fills, "read_order_fills", fill)
    monkeypatch.setattr(xstocks_ledgers, "read_fill_ledgers", ledger)
    monkeypatch.setattr(XStocksClient, "identity_metadata", metadata)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return SimpleNamespace(order=order, fill=fill, ledger=ledger, metadata=metadata)


def invoke(extra=None, units="base"):
    return CliRunner().invoke(
        cli.app,
        [
            "run",
            "xstocks-order-status",
            "--units",
            units,
            "--order-id",
            "o1",
            *(extra if extra is not None else ["--check-ledgers"]),
        ],
    )


@pytest.mark.parametrize("units", ["base", "rebased"])
def test_ledger_flag_implies_fill_checks_and_preserves_asset_fees(readers, units):
    result = invoke(units=units)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["fill_check"]["status"] == "volumes_match"
    assert body["fill_time_check"]["status"] == "times_match"
    assert body["ledger_check"]["status"] == "amounts_match"
    report = body["ledger_check"]["fills"]["t1"]
    assert report["fees_by_asset"] == {"AAPLX": "0", "USD": "-0.1"}
    assert report["other_fees_by_asset_id"] == {"KFEE": "0.2"}
    assert body["ledgers"] == LEDGERS
    assert body["fills"]["t1"]["fee"] == "999"
    assert body["identities"]["fill_pairs"] == {"t1": "AAPLX/USD"}
    assert body["started_at"] <= body["order_status_completed_at"] <= body["completed_at"]
    readers.fill.assert_awaited_once_with("k", "s", units=units, orders={"o1": ORDER})
    readers.ledger.assert_awaited_once_with("k", "s", units=units, fills={"t1": FILL})
    readers.metadata.assert_awaited_once()


@pytest.mark.parametrize("change", ["principal", "unknown_pair", "margin", "wrong_ref", "third_principal"])
def test_ledger_review_overrides_matching_volume_and_time(readers, change):
    if change == "principal":
        readers.ledger.return_value["base"]["amount"] = "0.4"
    elif change == "unknown_pair":
        readers.fill.return_value["t1"]["pair"] = "unknown"
    elif change == "margin":
        readers.fill.return_value["t1"]["margin"] = "1"
    elif change == "wrong_ref":
        readers.ledger.return_value["quote"]["refid"] = "other"
    else:
        readers.ledger.return_value["fee"]["amount"] = "1"
    result = invoke(["--check-ledgers", "--expected-expiry", "200"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["ledger_check"]["status"] == "needs_review"
    assert body["fill_check"]["status"] == "volumes_match"
    assert body["fill_time_check"]["status"] == "times_match"
    assert body["expiry_check"]["status"] == "terminal_by_deadline"


@pytest.mark.parametrize(
    "change,report", [("volume", "fill_check"), ("time", "fill_time_check"), ("expiry", "expiry_check")]
)
def test_other_checks_still_control_exit_when_ledgers_match(readers, change, report):
    if change == "volume":
        readers.order.return_value["o1"]["vol_exec"] = "0.4"
    elif change == "time":
        readers.fill.return_value["t1"]["time"] = 201
    else:
        readers.order.return_value["o1"]["expiretm"] = 201
    result = invoke(["--check-ledgers", "--expected-expiry", "200"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["ledger_check"]["status"] == "amounts_match"
    assert body[report]["status"] == "needs_review"


@pytest.mark.parametrize("stage", ["fill", "ledger", "metadata"])
def test_failed_reads_emit_no_partial_success(readers, stage):
    getattr(readers, stage).side_effect = ValueError("Request failed")
    result = invoke()
    assert result.exit_code == 1
    assert "order_status_read" not in result.output
    if stage == "fill":
        readers.ledger.assert_not_awaited()
    if stage != "metadata":
        readers.metadata.assert_not_awaited()


def test_empty_execution_has_no_fills_or_metadata_claim(readers):
    readers.order.return_value = {"o1": {**ORDER, "trades": [], "vol_exec": "0"}}
    readers.fill.return_value = {}
    readers.ledger.return_value = {}
    result = invoke()
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["ledger_check"]["status"] == "no_fills"
    assert body["fill_time_check"]["status"] == "no_fills"
    assert body["ledgers"] == {} and body["fills"] == {}
    readers.metadata.assert_not_awaited()


@pytest.mark.parametrize("args", [[], ["--check-fills"]])
def test_ledger_fetch_remains_opt_in(readers, args):
    result = invoke(args)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert "ledgers" not in body and "ledger_check" not in body and "identities" not in body
    readers.ledger.assert_not_awaited()
    readers.metadata.assert_not_awaited()
