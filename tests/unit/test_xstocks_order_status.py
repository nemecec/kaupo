import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_order_status as status

ORDER = {
    "status": "expired",
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit"},
    "vol": "2",
    "vol_exec": "0.5",
    "expiretm": 200,
    "opentm": 100,
    "closetm": 200.1,
    "trades": ["trade-1"],
    "reason": "Order expired",
}


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_actual_signed_transport_preserves_terminal_fields(monkeypatch, units):
    exchange = status.ccxt.kraken({"apiKey": "test-key", "secret": "c2VjcmV0", "enableRateLimit": False})
    orders = {"order-1": ORDER, "order-2": {**ORDER, "status": "closed", "vol_exec": "2"}}
    fetch = AsyncMock(return_value={"error": [], "result": orders})
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(status.ccxt, "kraken", lambda config: exchange)
    assert await status.read_order_status("k", "s", units=units, order_ids=list(orders)) == orders
    url, method, headers, body = fetch.call_args.args
    assert url == "https://api.kraken.com/0/private/QueryOrders"
    assert method == "POST" and headers["API-Sign"]
    params = parse_qs(body)
    assert params.pop("nonce")
    assert params == {
        "txid": ["order-1,order-2"],
        "rebase_multiplier": [units],
        "trades": ["true"],
        "consolidate_taker": ["false"],
    }


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(
        private_post_queryorders=AsyncMock(return_value={"error": [], "result": {"id": ORDER}}),
        close=AsyncMock(),
    )
    monkeypatch.setattr(status.ccxt, "kraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize("state", ["open", "pending", "closed", "canceled", "expired"])
async def test_all_documented_states_and_partial_terminal_fills(client, state):
    order = {**ORDER, "status": state}
    if state in ("open", "pending"):
        del order["closetm"]
    client.private_post_queryorders.return_value["result"] = {"id": order}
    assert await status.read_order_status("k", "s", units="base", order_ids=["id"]) == {"id": order}
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": {}},
        {"error": [], "result": {"id": ORDER, "extra": ORDER}},
        {"error": [], "result": {"different": ORDER}},
    ],
)
async def test_missing_extra_and_rejected_results_fail(client, response):
    client.private_post_queryorders.return_value = response
    with pytest.raises(ValueError):
        await status.read_order_status("k", "s", units="base", order_ids=["id"])
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "unknown"),
        ("descr", {}),
        ("vol", "0"),
        ("vol_exec", "3"),
        ("expiretm", "NaN"),
        ("opentm", True),
        ("closetm", None),
        ("closetm", 99),
        ("closetm", "bad"),
        ("closetm", "Infinity"),
        ("trades", "id"),
        ("trades", [""]),
        ("trades", [1]),
        ("trades", ["t", "t"]),
    ],
)
async def test_invalid_core_fields_fail(client, field, value):
    client.private_post_queryorders.return_value["result"] = {"id": {**ORDER, field: value}}
    with pytest.raises(ValueError):
        await status.read_order_status("k", "s", units="base", order_ids=["id"])
    client.close.assert_awaited_once()


@pytest.mark.parametrize("ids", [[], ["a"] * 51, ["a", "a"], [""], ["a,b"], ["a b"], [None], "id"])
def test_invalid_request(ids):
    with pytest.raises(ValueError):
        status.check_order_request(ids, "base")


def test_maximum_request_and_units():
    status.check_order_request([f"id-{i}" for i in range(50)], "rebased")
    with pytest.raises(ValueError):
        status.check_order_request(["id"], "wrong")


async def test_transport_and_close_failures_are_sanitized(client):
    client.private_post_queryorders.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Kraken order-status request failed \(RuntimeError\)$"):
        await status.read_order_status("k", "s", units="base", order_ids=["id"])
    client.close.assert_awaited_once()
    client.private_post_queryorders.side_effect = None
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Could not close the Kraken order-status client$"):
        await status.read_order_status("k", "s", units="base", order_ids=["id"])


@pytest.mark.parametrize("key,secret", [("", "s"), ("k", "")])
async def test_missing_credentials(client, key, secret):
    with pytest.raises(ValueError, match="credentials"):
        await status.read_order_status(key, secret, units="base", order_ids=["id"])
    client.private_post_queryorders.assert_not_awaited()


@pytest.fixture
def reader(monkeypatch):
    read = AsyncMock(return_value={"one": ORDER, "two": ORDER})
    monkeypatch.setattr(status, "read_order_status", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return read


def invoke(args):
    return CliRunner().invoke(cli.app, ["run", "xstocks-order-status", *args])


def test_cli_repeat_ids_and_explicit_scope(reader):
    result = invoke(["--units", "base", "--order-id", "one", "--order-id", "two"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "order_status_read"
    assert body["orders"] == {"one": ORDER, "two": ORDER}
    assert body["requested_order_ids"] == ["one", "two"]
    assert body["rebase_multiplier"] == "base" and body["wallet"] == "default"
    assert body["started_at"] <= body["completed_at"]
    reader.assert_awaited_once_with("k", "s", units="base", order_ids=["one", "two"])


@pytest.mark.parametrize(
    "args",
    [
        ["--units", "base"],
        ["--order-id", "one"],
        ["--units", "bad", "--order-id", "one"],
        ["--units", "base", "--order-id", "a,b"],
    ],
)
def test_cli_invalid_before_read(reader, args):
    result = invoke(args)
    assert result.exit_code == 2
    reader.assert_not_awaited()


def test_cli_read_error_has_no_success_json(reader):
    reader.side_effect = ValueError("Requested order missing")
    result = invoke(["--units", "base", "--order-id", "one"])
    assert result.exit_code == 1
    assert "order_status_read" not in result.output
