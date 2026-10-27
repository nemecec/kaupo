from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_account as account

ORDER = {
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit", "price": "123.45"},
    "status": "open",
    "vol": "1.000000000000001",
    "vol_exec": "0.1",
    "expiretm": 1793000000,
}


def page(orders, cursor=None):
    return {"error": [], "result": {"open": orders, "cursor": {"next": cursor}}}


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_signed_pagination_keeps_symbols_volumes_and_expiry(monkeypatch, units):
    exchange = account.ccxt.kraken({"apiKey": "test-key", "secret": "c2VjcmV0"})
    other = {**ORDER, "descr": {**ORDER["descr"], "pair": "XBTUSD"}, "expiretm": 0}
    fetch = AsyncMock(side_effect=[page({"one": ORDER}, "opaque/+="), page({"two": other})])
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    assert await account.read_open_orders("key", "secret", units=units) == {"one": ORDER, "two": other}
    assert fetch.await_count == 2
    for i, call in enumerate(fetch.call_args_list):
        url, method, headers, body = call.args
        assert url == "https://api.kraken.com/0/private/OpenOrders"
        assert method == "POST"
        assert headers["API-Sign"]
        expected = {
            "rebase_multiplier": [units],
            "with_cursor": ["true"],
            "limit": ["100"],
            "trades": ["false"],
        }
        if i:
            expected["cursor"] = ["opaque/+="]
        parsed = parse_qs(body)
        assert parsed.pop("nonce")
        assert parsed == expected


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(private_post_openorders=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": {}},
        page({"": ORDER}),
        page({"id": None}),
        page({"id": {}}),
        {"error": [], "result": {"open": {}, "cursor": []}},
        page({}, 12),
        page({}, " "),
    ],
)
async def test_malformed_responses_fail_and_close(client, response):
    client.private_post_openorders.return_value = response
    with pytest.raises(ValueError):
        await account.read_open_orders("key", "secret", units="base")
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("vol", "0"),
        ("vol", "NaN"),
        ("vol", 1),
        ("vol_exec", "2"),
        ("vol_exec", "-1"),
        ("expiretm", True),
        ("expiretm", "bad"),
        ("expiretm", float("inf")),
        ("status", "closed"),
        ("descr", {"pair": ""}),
    ],
)
async def test_invalid_order_fields_fail(client, field, value):
    client.private_post_openorders.return_value = page({"id": {**ORDER, field: value}})
    with pytest.raises(ValueError):
        await account.read_open_orders("key", "secret", units="base")


async def test_empty_response_and_identical_overlap(client):
    client.private_post_openorders.return_value = {"error": [], "result": {"open": {}}}
    assert await account.read_open_orders("key", "secret", units="base") == {}
    client.private_post_openorders.side_effect = [page({"one": ORDER}, "a"), page({"one": deepcopy(ORDER)})]
    assert await account.read_open_orders("key", "secret", units="base") == {"one": ORDER}


@pytest.mark.parametrize(
    "pages",
    [
        [page({}, "a")],
        [page({"one": ORDER}, "a"), page({"two": ORDER}, "a")],
        [page({"one": ORDER}, "a"), page({"one": ORDER}, "b")],
        [page({"one": ORDER}, "a"), page({"one": {**ORDER, "vol_exec": "0.2"}})],
    ],
)
async def test_incomplete_or_changed_pages_never_return_partial_success(client, pages):
    client.private_post_openorders.side_effect = pages
    with pytest.raises(ValueError):
        await account.read_open_orders("key", "secret", units="base")
    client.close.assert_awaited_once()


async def test_page_bound(client):
    client.private_post_openorders.side_effect = [page({str(i): ORDER}, str(i)) for i in range(100)]
    with pytest.raises(ValueError, match="exceeded 100 pages"):
        await account.read_open_orders("key", "secret", units="base")
    assert client.private_post_openorders.await_count == 100


async def test_transport_and_close_errors_are_sanitized(client):
    client.private_post_openorders.side_effect = RuntimeError("secret signed request")
    with pytest.raises(ValueError, match=r"^Kraken open-order request failed \(RuntimeError\)$"):
        await account.read_open_orders("key", "secret", units="base")
    client.private_post_openorders.assert_awaited_once()
    client.close.assert_awaited_once()
    client.private_post_openorders.side_effect = None
    client.private_post_openorders.return_value = page({})
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Could not close the Kraken open-order client$"):
        await account.read_open_orders("key", "secret", units="base")


@pytest.mark.parametrize("key,secret,units", [("", "s", "base"), ("k", "", "base"), ("k", "s", "bad")])
async def test_invalid_inputs_before_client(monkeypatch, key, secret, units):
    def unexpected(config):
        pytest.fail("Client must not be constructed")

    monkeypatch.setattr(account.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await account.read_open_orders(key, secret, units=units)


@pytest.mark.parametrize("units", ["base", "rebased"])
def test_cli_open_order_evidence(monkeypatch, units):
    import json

    read = AsyncMock(return_value={"order-id": ORDER})
    monkeypatch.setattr(account, "read_open_orders", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(cli.app, ["run", "xstocks-open-orders", "--units", units])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["rebase_multiplier"] == units
    assert body["open_orders"] == {"order-id": ORDER}
    assert body["wallet"] == "default"
    assert body["status"] == "open_orders_read"
    assert body["started_at"] <= body["completed_at"]
    read.assert_awaited_once_with("key", "s", units=units)


@pytest.mark.parametrize("options", [[], ["--units", "wrong"], ["--units", "base"]])
def test_cli_rejects_missing_units_invalid_units_and_credentials(monkeypatch, options):
    read = AsyncMock()
    monkeypatch.setattr(account, "read_open_orders", read)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(kraken_api_key="", kraken_api_secret=""))
    result = CliRunner().invoke(cli.app, ["run", "xstocks-open-orders", *options])
    assert result.exit_code == 2
    read.assert_not_awaited()


def test_cli_read_failure_emits_no_success_json(monkeypatch):
    monkeypatch.setattr(
        account, "read_open_orders", AsyncMock(side_effect=ValueError("Kraken rejected request"))
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(cli.app, ["run", "xstocks-open-orders", "--units", "base"])
    assert result.exit_code == 1
    assert "Open-order read failed" in result.output
    assert "open_orders_read" not in result.output
