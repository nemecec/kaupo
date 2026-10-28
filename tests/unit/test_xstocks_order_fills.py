import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_order_fills as module
from kaupo.venues import xstocks_order_status

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
    "fee": "-0.1",
    "ledgers": ["l1", "l2"],
}


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_signed_batches_preserve_units_and_references(monkeypatch, units):
    exchange = module.ccxt.kraken({"apiKey": "key", "secret": "c2VjcmV0", "enableRateLimit": False})
    ids = [f"t{i}" for i in range(21)]
    fills = {i: deepcopy(FILL) for i in ids}
    fetch = AsyncMock(
        side_effect=[
            {"error": [], "result": {i: fills[i] for i in ids[:20]}},
            {"error": [], "result": {ids[-1]: fills[ids[-1]]}},
        ]
    )
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(module.ccxt, "kraken", lambda config: exchange)
    result = await module.read_order_fills("k", "s", units=units, orders={"o1": {**ORDER, "trades": ids}})
    assert result == fills
    for n, call in enumerate(fetch.call_args_list):
        url, method, headers, body = call.args
        assert url == "https://api.kraken.com/0/private/QueryTrades"
        assert method == "POST" and headers["API-Sign"]
        params = parse_qs(body)
        assert params.pop("nonce")
        assert params == {
            "txid": [",".join(ids[n * 20 : (n + 1) * 20])],
            "rebase_multiplier": [units],
            "trades": ["false"],
            "ledgers": ["true"],
        }


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(
        private_post_querytrades=AsyncMock(return_value={"error": [], "result": {"t1": FILL}}),
        close=AsyncMock(),
    )
    monkeypatch.setattr(module.ccxt, "kraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": {}},
        {"error": [], "result": {"other": FILL}},
        {"error": [], "result": {"t1": FILL, "extra": FILL}},
    ],
)
async def test_missing_extra_and_api_errors(client, response):
    client.private_post_querytrades.return_value = response
    with pytest.raises(ValueError):
        await module.read_order_fills("k", "s", units="base", orders={"o1": ORDER})
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value", [("ordertxid", "other"), ("type", "sell"), ("vol", "NaN"), ("price", "0"), ("time", -1)]
)
async def test_invalid_fills_and_wrong_owner(client, field, value):
    client.private_post_querytrades.return_value["result"] = {"t1": {**FILL, field: value}}
    with pytest.raises(ValueError):
        await module.read_order_fills("k", "s", units="base", orders={"o1": ORDER})
    client.close.assert_awaited_once()


@pytest.mark.parametrize("refs", [[], ["a,b"], ["a b"], ["a", "a"], [f"t{i}" for i in range(2001)]])
async def test_bad_references_before_request(client, refs):
    with pytest.raises(ValueError):
        await module.read_order_fills("k", "s", units="base", orders={"o1": {**ORDER, "trades": refs}})
    client.private_post_querytrades.assert_not_awaited()


def test_shared_references_rejected():
    with pytest.raises(ValueError, match="multiple orders"):
        module.order_trade_owners({"o1": ORDER, "o2": ORDER})


async def test_zero_execution_needs_no_client(client):
    orders = {"o1": {**ORDER, "trades": [], "vol_exec": "0"}}
    assert await module.read_order_fills("k", "s", units="base", orders=orders) == {}
    client.private_post_querytrades.assert_not_awaited()
    client.close.assert_not_awaited()
    assert module.check_order_fill_volumes(orders, {})["status"] == "volumes_match"


async def test_later_batch_failure_has_no_partial_success(client):
    ids = [f"t{i}" for i in range(21)]
    client.private_post_querytrades.side_effect = [
        {"error": [], "result": {i: FILL for i in ids[:20]}},
        RuntimeError("secret"),
    ]
    with pytest.raises(ValueError, match=r"^Kraken order-fill request failed \(RuntimeError\)$"):
        await module.read_order_fills("k", "s", units="base", orders={"o1": {**ORDER, "trades": ids}})
    client.close.assert_awaited_once()


async def test_close_error_sanitized(client):
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match="Could not close the Kraken order-fill client"):
        await module.read_order_fills("k", "s", units="base", orders={"o1": ORDER})


@pytest.mark.parametrize("units,key,secret", [("bad", "k", "s"), ("base", "", "s"), ("base", "k", "")])
async def test_invalid_scope_before_network(client, units, key, secret):
    with pytest.raises(ValueError):
        await module.read_order_fills(key, secret, units=units, orders={"o1": ORDER})
    client.private_post_querytrades.assert_not_awaited()


@pytest.mark.parametrize(
    "volume,expected",
    [("0.5", "volume_match"), ("0.4", "volume_mismatch"), ("1e100", "unresolved"), ("0", "unresolved")],
)
def test_volume_comparison(volume, expected):
    report = module.check_order_fill_volumes({"o1": ORDER}, {"t1": {**FILL, "vol": volume}})
    assert report["orders"]["o1"]["status"] == expected
    assert report["status"] == ("volumes_match" if expected == "volume_match" else "needs_review")


def test_exact_sum_and_separate_orders():
    orders = {
        "o1": {**ORDER, "vol_exec": "0.50000000000000000001", "trades": ["t1", "t2"]},
        "o2": {**ORDER, "trades": ["t3"]},
    }
    fills = {"t1": FILL, "t2": {**FILL, "vol": "0.00000000000000000001"}, "t3": {**FILL, "ordertxid": "o2"}}
    assert module.check_order_fill_volumes(orders, fills)["status"] == "volumes_match"
    fills["t3"]["ordertxid"] = "o1"
    assert (
        module.check_order_fill_volumes(orders, fills)["orders"]["o2"]["reason"] == "fill_identity_mismatch"
    )
    with pytest.raises(ValueError, match="exactly"):
        module.check_order_fill_volumes(orders, {})


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(xstocks_order_status, "read_order_status", AsyncMock(return_value={"o1": ORDER}))
    read = AsyncMock(return_value={"t1": FILL})
    monkeypatch.setattr(module, "read_order_fills", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return read


def invoke(extra):
    return CliRunner().invoke(
        cli.app, ["run", "xstocks-order-status", "--units", "base", "--order-id", "o1", *extra]
    )


@pytest.mark.parametrize("volume,code", [("0.5", 0), ("0.4", 1)])
def test_cli_check_fills_and_expiry(reader, volume, code):
    reader.return_value = {"t1": {**FILL, "vol": volume}}
    result = invoke(["--check-fills", "--expected-expiry", "200"])
    assert result.exit_code == code, result.output
    body = json.loads(result.stdout)
    assert body["expiry_check"]["status"] == "terminal_by_deadline"
    assert body["fill_check"]["orders"]["o1"]["fill_volume"] == volume
    assert body["fills"] == reader.return_value
    assert body["started_at"] <= body["order_status_completed_at"] <= body["completed_at"]
    reader.assert_awaited_once_with("k", "s", units="base", orders={"o1": ORDER})


def test_cli_no_success_on_lookup_failure(reader):
    reader.side_effect = ValueError("Missing trade")
    result = invoke(["--check-fills"])
    assert result.exit_code == 1
    assert "order_status_read" not in result.output


def test_default_cli_does_not_fetch_fills(reader):
    result = invoke([])
    assert result.exit_code == 0
    assert "fills" not in json.loads(result.stdout)
    reader.assert_not_awaited()


def test_expiry_interval_ends_before_fill_lookup(reader, monkeypatch):
    from datetime import datetime

    times = iter([199, 199.5, 201])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(next(times), tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(
        xstocks_order_status, "read_order_status", AsyncMock(return_value={"o1": {**ORDER, "status": "open"}})
    )
    result = invoke(["--check-fills", "--expected-expiry", "200"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["expiry_check"]["reason"] == "deadline_not_reached"
    assert body["fill_check"]["status"] == "volumes_match"
