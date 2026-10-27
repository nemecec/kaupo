import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_ledger_history as history

ENTRY = {
    "asset": "AAPLx",
    "aclass": "tokenized_asset",
    "type": "deposit",
    "time": 150,
    "amount": "2",
    "fee": "0",
    "balance": "2",
}
KW = {"asset": "AAPLx", "asset_class": "tokenized_asset", "units": "base", "start": 100, "end": 200}


def page(entries, cursor=None):
    return {"error": [], "result": {"ledger": entries, "cursor": {"next": cursor}, "count": None}}


@pytest.mark.parametrize("units", ["base", "rebased"])
@pytest.mark.parametrize("asset,asset_class", [("AAPLx", "tokenized_asset"), ("ZUSD", "currency")])
async def test_signed_pagination_includes_nontrade_activity(monkeypatch, units, asset, asset_class):
    entry = {**ENTRY, "asset": asset, "aclass": asset_class}
    exchange = history._LedgerKraken({"apiKey": "test-key", "secret": "c2VjcmV0", "enableRateLimit": False})
    other = {**entry, "time": 200, "type": "adjustment", "amount": "-1", "balance": "1"}
    fetch = AsyncMock(side_effect=[page({"one": entry}, "opaque/+="), page({"two": other})])
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(history, "_LedgerKraken", lambda config: exchange)
    assert await history.read_ledger_history(
        "key", "secret", **{**KW, "units": units, "asset": asset, "asset_class": asset_class}
    ) == {
        "one": entry,
        "two": other,
    }
    for i, call in enumerate(fetch.call_args_list):
        url, method, headers, body = call.args
        assert url == "https://api.kraken.com/0/private/Ledgers"
        assert method == "POST" and headers["API-Sign"]
        params = json.loads(body)
        nonce = params.pop("nonce")
        assert isinstance(nonce, int)
        digest = hashlib.sha256((str(nonce) + body).encode()).digest()
        expected_sign = hmac.digest(b"secret", b"/0/private/Ledgers" + digest, "sha512")
        assert headers == {
            "API-Key": "test-key",
            "API-Sign": base64.b64encode(expected_sign).decode(),
            "Content-Type": "application/json",
        }
        expected = {
            "rebase_multiplier": units,
            "with_cursor": True,
            "asset": [{"asset": asset, "aclass": asset_class}],
            "type": "all",
            "start": 100,
            "end": 200,
        }
        if i:
            expected["cursor"] = "opaque/+="
        assert params == expected
    assert fetch.await_count == 2


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(private_post_ledgers=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(history, "_LedgerKraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": {}},
        page({"": ENTRY}),
        page({"id": None}),
        page({"id": {**ENTRY, "amount": "NaN"}}),
        page({"id": {**ENTRY, "asset": "other"}}),
        page({"id": {**ENTRY, "aclass": "currency"}}),
        page({"id": {**ENTRY, "time": 100}}),
        page({"id": {**ENTRY, "time": 201}}),
        {"error": [], "result": {"ledger": {}, "cursor": []}},
        page({}, 12),
    ],
)
async def test_invalid_response_fails_and_closes(client, response):
    client.private_post_ledgers.return_value = response
    with pytest.raises(ValueError):
        await history.read_ledger_history("key", "secret", **KW)
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "pages",
    [
        [page({}, "a")],
        [page({"one": ENTRY}, "a"), page({"two": ENTRY}, "a")],
        [page({"one": ENTRY}, "a"), page({"one": ENTRY}, "b")],
        [page({"one": ENTRY}, "a"), page({"one": {**ENTRY, "balance": "3"}})],
    ],
)
async def test_stalls_and_changed_entries_fail(client, pages):
    client.private_post_ledgers.side_effect = pages
    with pytest.raises(ValueError):
        await history.read_ledger_history("key", "secret", **KW)
    client.close.assert_awaited_once()


async def test_empty_and_duplicate_entries(client):
    client.private_post_ledgers.return_value = {"error": [], "result": {"ledger": {}}}
    assert await history.read_ledger_history("key", "secret", **KW) == {}
    client.private_post_ledgers.side_effect = [page({"one": ENTRY}, "a"), page({"one": ENTRY})]
    assert await history.read_ledger_history("key", "secret", **KW) == {"one": ENTRY}


async def test_page_bound_and_sanitized_errors(client):
    client.private_post_ledgers.side_effect = [page({str(i): ENTRY}, str(i)) for i in range(100)]
    with pytest.raises(ValueError, match="exceeded 100 pages"):
        await history.read_ledger_history("key", "secret", **KW)
    assert client.private_post_ledgers.await_count == 100
    client.private_post_ledgers.side_effect = [page({"one": ENTRY}, "a"), RuntimeError("secret")]
    with pytest.raises(ValueError, match=r"^Kraken ledger history request failed \(RuntimeError\)$"):
        await history.read_ledger_history("key", "secret", **KW)
    client.private_post_ledgers.side_effect = None
    client.private_post_ledgers.return_value = page({})
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match="Could not close"):
        await history.read_ledger_history("key", "secret", **KW)


@pytest.mark.parametrize(
    "changes",
    [
        {"asset": ""},
        {"asset": "all"},
        {"asset": "a,b"},
        {"asset": "a b"},
        {"asset_class": "bad"},
        {"units": "bad"},
        {"start": 200},
        {"end": 31 * 86400 + 101},
    ],
)
async def test_invalid_request_before_client(monkeypatch, changes):
    def unexpected(config):
        pytest.fail("Must reject before constructing client")

    monkeypatch.setattr(history, "_LedgerKraken", unexpected)
    with pytest.raises(ValueError):
        await history.read_ledger_history("key", "secret", **{**KW, **changes})


@pytest.mark.parametrize("key,secret", [("", "s"), ("k", "")])
async def test_credentials_required(key, secret):
    with pytest.raises(ValueError, match="credentials"):
        await history.read_ledger_history(key, secret, **KW)


@pytest.mark.parametrize("check,balance,exit_code", [(False, "2", 0), (True, "2", 0), (True, "3", 1)])
def test_cli_balance_links_and_scope(monkeypatch, check, balance, exit_code):
    import json

    read = AsyncMock(
        return_value={
            "one": ENTRY,
            "two": {**ENTRY, "time": 200, "type": "withdrawal", "amount": "0", "balance": balance},
        }
    )
    monkeypatch.setattr(history, "read_ledger_history", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    args = [
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
    ]
    result = CliRunner().invoke(cli.app, args + (["--check-balance-links"] if check else []))
    assert result.exit_code == exit_code, result.output
    body = json.loads(result.stdout)
    assert body["asset"] == "AAPLx" and body["asset_class"] == "tokenized_asset"
    if check:
        assert body["balance_links"]["scope"] == "asset_window"
    else:
        assert "balance_links" not in body
    read.assert_awaited_once_with("k", "s", **KW)


@pytest.mark.parametrize(
    "asset,credentials,fail,code",
    [("all", True, False, 2), ("AAPLx", False, False, 2), ("AAPLx", True, True, 1)],
)
def test_cli_errors_never_emit_success(monkeypatch, asset, credentials, fail, code):
    read = AsyncMock(side_effect=ValueError("Read failed") if fail else None)
    monkeypatch.setattr(history, "read_ledger_history", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            kraken_api_key="k" if credentials else "", kraken_api_secret="s" if credentials else ""
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "xstocks-ledgers",
            "--asset",
            asset,
            "--asset-class",
            "tokenized_asset",
            "--units",
            "base",
            "--start",
            "100",
            "--end",
            "200",
        ],
    )
    assert result.exit_code == code
    assert "ledger_history_read" not in result.output
    assert read.await_count == int(fail)


async def test_other_endpoints_keep_ccxt_signing():
    exchange = history._LedgerKraken({"apiKey": "test-key", "secret": "c2VjcmV0"})
    try:
        signed = exchange.sign("Balance", "private", "POST", {"rebase_multiplier": "base"})
        assert signed["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert parse_qs(signed["body"])["rebase_multiplier"] == ["base"]
        public = exchange.sign("Time")
        assert public["url"] == "https://api.kraken.com/0/public/Time"
        assert public["headers"] is None
    finally:
        await exchange.close()
