from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_fills as account

FILL = {
    "ordertxid": "order-id",
    "pair": "AAPLxUSD",
    "type": "buy",
    "ordertype": "limit",
    "price": "123.45",
    "vol": "1.000000000000001",
    "cost": "123.45000000000012345",
    "fee": "-0.02469",
    "time": 150.125,
    "trade_id": 123,
    "ledgers": ["ledger-id"],
}


def page(fills, cursor=None):
    return {"error": [], "result": {"trades": fills, "cursor": {"next": cursor}}}


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_signed_pagination_keeps_ids_amounts_rebates_and_boundaries(monkeypatch, units):
    exchange = account.ccxt.kraken({"apiKey": "test-key", "secret": "c2VjcmV0"})
    other = {**FILL, "pair": "XBTUSD", "type": "sell", "time": 200}
    fetch = AsyncMock(side_effect=[page({"one": FILL}, "opaque/+="), page({"two": other})])
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    assert await account.read_fills("key", "secret", units=units, start=100, end=200) == {
        "one": FILL,
        "two": other,
    }
    assert fetch.await_count == 2
    for i, call in enumerate(fetch.call_args_list):
        url, method, headers, body = call.args
        assert url == "https://api.kraken.com/0/private/TradesHistory"
        assert method == "POST"
        assert headers["API-Sign"]
        expected = {
            "rebase_multiplier": [units],
            "with_cursor": ["true"],
            "limit": ["100"],
            "trades": ["false"],
            "consolidate_taker": ["false"],
            "ledgers": ["true"],
            "type": ["all"],
            "start": ["100"],
            "end": ["200"],
        }
        if i:
            expected["cursor"] = ["opaque/+="]
        parsed = parse_qs(body)
        assert parsed.pop("nonce")
        assert parsed == expected


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(private_post_tradeshistory=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": {}},
        page({"": FILL}),
        page({"id": None}),
        page({"id": {}}),
        {"error": [], "result": {"trades": {}, "cursor": []}},
        page({}, 12),
        page({}, " "),
    ],
)
async def test_malformed_responses_fail_and_close(client, response):
    client.private_post_tradeshistory.return_value = response
    with pytest.raises(ValueError):
        await account.read_fills("key", "secret", units="base", start=100, end=200)
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("vol", "0"),
        ("price", "-1"),
        ("cost", "-1"),
        ("fee", "NaN"),
        ("fee", "bad"),
        ("price", 1),
        ("time", True),
        ("time", 100),
        ("time", 201),
        ("time", float("inf")),
        ("type", "invalid"),
        ("pair", ""),
        ("ordertxid", None),
    ],
)
async def test_invalid_fill_fields_fail(client, field, value):
    client.private_post_tradeshistory.return_value = page({"id": {**FILL, field: value}})
    with pytest.raises(ValueError):
        await account.read_fills("key", "secret", units="base", start=100, end=200)


async def test_empty_response_and_identical_overlap(client):
    client.private_post_tradeshistory.return_value = {"error": [], "result": {"trades": {}}}
    assert await account.read_fills("key", "secret", units="base", start=100, end=200) == {}
    client.private_post_tradeshistory.side_effect = [page({"one": FILL}, "a"), page({"one": deepcopy(FILL)})]
    assert await account.read_fills("key", "secret", units="base", start=100, end=200) == {"one": FILL}


@pytest.mark.parametrize(
    "pages",
    [
        [page({}, "a")],
        [page({"one": FILL}, "a"), page({"two": FILL}, "a")],
        [page({"one": FILL}, "a"), page({"one": FILL}, "b")],
        [page({"one": FILL}, "a"), page({"one": {**FILL, "fee": "0.2"}})],
    ],
)
async def test_incomplete_or_changed_pages_never_return_partial_success(client, pages):
    client.private_post_tradeshistory.side_effect = pages
    with pytest.raises(ValueError):
        await account.read_fills("key", "secret", units="base", start=100, end=200)
    client.close.assert_awaited_once()


async def test_page_bound(client):
    client.private_post_tradeshistory.side_effect = [page({str(i): FILL}, str(i)) for i in range(100)]
    with pytest.raises(ValueError, match="exceeded 100 pages"):
        await account.read_fills("key", "secret", units="base", start=100, end=200)
    assert client.private_post_tradeshistory.await_count == 100


async def test_transport_and_close_errors_are_sanitized(client):
    client.private_post_tradeshistory.side_effect = RuntimeError("secret signed request")
    with pytest.raises(ValueError, match=r"^Kraken fill request failed \(RuntimeError\)$"):
        await account.read_fills("key", "secret", units="base", start=100, end=200)
    client.private_post_tradeshistory.assert_awaited_once()
    client.close.assert_awaited_once()
    client.private_post_tradeshistory.side_effect = None
    client.private_post_tradeshistory.return_value = page({})
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Could not close the Kraken fill client$"):
        await account.read_fills("key", "secret", units="base", start=100, end=200)


@pytest.mark.parametrize("key,secret,units", [("", "s", "base"), ("k", "", "base"), ("k", "s", "bad")])
async def test_invalid_inputs_before_client(monkeypatch, key, secret, units):
    def unexpected(config):
        pytest.fail("Client must not be constructed")

    monkeypatch.setattr(account.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await account.read_fills(key, secret, units=units, start=100, end=200)


@pytest.mark.parametrize("units", ["base", "rebased"])
def test_cli_fill_evidence(monkeypatch, units):
    import json

    read = AsyncMock(return_value={"fill-id": FILL})
    monkeypatch.setattr(account, "read_fills", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(
        cli.app, ["run", "xstocks-fills", "--units", units, "--start", "100", "--end", "200"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["rebase_multiplier"] == units
    assert body["fills"] == {"fill-id": FILL}
    assert body["start_exclusive"] == 100
    assert body["end_inclusive"] == 200
    assert body["consolidate_taker"] is False
    assert body["wallet"] == "default"
    assert body["status"] == "fills_read"
    assert body["started_at"] <= body["completed_at"]
    read.assert_awaited_once_with("key", "s", units=units, start=100, end=200)


@pytest.mark.parametrize(
    "options", [[], ["--units", "wrong"], ["--units", "base", "--start", "100", "--end", "200"]]
)
def test_cli_rejects_missing_units_invalid_units_and_credentials(monkeypatch, options):
    read = AsyncMock()
    monkeypatch.setattr(account, "read_fills", read)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(kraken_api_key="", kraken_api_secret=""))
    result = CliRunner().invoke(cli.app, ["run", "xstocks-fills", *options])
    assert result.exit_code == 2
    read.assert_not_awaited()


def test_cli_read_failure_emits_no_success_json(monkeypatch):
    monkeypatch.setattr(account, "read_fills", AsyncMock(side_effect=ValueError("Kraken rejected request")))
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(
        cli.app, ["run", "xstocks-fills", "--units", "base", "--start", "100", "--end", "200"]
    )
    assert result.exit_code == 1
    assert "Fill read failed" in result.output
    assert "fills_read" not in result.output


@pytest.mark.parametrize(
    "start,end",
    [
        (-1, 100),
        (100, 100),
        (200, 100),
        (True, 100),
        (1.5, 100),
        (0, 31 * 86400 + 1),
        (253402300798, 253402300800),
    ],
)
async def test_invalid_window_before_client(monkeypatch, start, end):
    def unexpected(config):
        pytest.fail("Client must not be constructed")

    monkeypatch.setattr(account.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await account.read_fills("key", "secret", units="base", start=start, end=end)


def test_maximum_window_is_accepted():
    account.check_fill_window(0, 31 * 86400)


@pytest.mark.parametrize("start,end", [(100, 100), (-1, 100), (0, 31 * 86400 + 1)])
def test_cli_invalid_window_before_network(monkeypatch, start, end):
    read = AsyncMock()
    monkeypatch.setattr(account, "read_fills", read)
    result = CliRunner().invoke(
        cli.app, ["run", "xstocks-fills", "--units", "base", "--start", str(start), "--end", str(end)]
    )
    assert result.exit_code == 2
    read.assert_not_awaited()


async def test_later_transport_failure_discards_partial_collection(client):
    client.private_post_tradeshistory.side_effect = [page({"one": FILL}, "next"), RuntimeError("secret")]
    with pytest.raises(ValueError, match="request failed"):
        await account.read_fills("key", "secret", units="base", start=100, end=200)
    assert client.private_post_tradeshistory.await_count == 2
    client.close.assert_awaited_once()
