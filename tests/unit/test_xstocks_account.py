from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_account as account


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_signed_balance_request_preserves_assets_and_decimal_strings(monkeypatch, units):
    exchange = account.ccxt.kraken({"apiKey": "test-key", "secret": "c2VjcmV0"})
    balances = {"AAPL.T": "0.12345678901234567890", "ZUSD": "-1.50", "USD.F": "0"}
    fetch = AsyncMock(return_value={"error": [], "result": balances})
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    assert await account.read_balances("test-key", "c2VjcmV0", units=units) == balances
    fetch.assert_awaited_once()
    url, method, headers, body = fetch.call_args.args
    assert url == "https://api.kraken.com/0/private/Balance"
    assert method == "POST"
    assert headers["API-Key"] == "test-key"
    assert set(parse_qs(body)) == {"nonce", "rebase_multiplier"}
    assert parse_qs(body)["rebase_multiplier"] == [units]


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["private error"]},
        {"error": [], "result": []},
        {"error": [], "result": {"": "1"}},
        {"error": [], "result": {"AAPL.T": 1}},
        {"error": [], "result": {"AAPL.T": "NaN"}},
        {"error": [], "result": {"AAPL.T": "Infinity"}},
        {"error": [], "result": {"AAPL.T": "bad"}},
    ],
)
async def test_bad_balance_response_fails_and_closes(monkeypatch, response):
    exchange = SimpleNamespace(private_post_balance=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    with pytest.raises(ValueError, match="Kraken"):
        await account.read_balances("key", "secret", units="rebased")
    exchange.close.assert_awaited_once()


async def test_transport_failure_is_sanitized_and_not_retried(monkeypatch):
    exchange = SimpleNamespace(
        private_post_balance=AsyncMock(side_effect=RuntimeError("signed request secret")), close=AsyncMock()
    )
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    with pytest.raises(ValueError, match=r"^Kraken balance request failed \(RuntimeError\)$"):
        await account.read_balances("key", "secret", units="rebased")
    exchange.private_post_balance.assert_awaited_once()
    exchange.close.assert_awaited_once()


async def test_empty_balances_and_close_failure(monkeypatch):
    exchange = SimpleNamespace(
        private_post_balance=AsyncMock(return_value={"error": [], "result": {}}), close=AsyncMock()
    )
    monkeypatch.setattr(account.ccxt, "kraken", lambda config: exchange)
    assert await account.read_balances("key", "secret", units="base") == {}
    exchange.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Could not close the Kraken balance client$"):
        await account.read_balances("key", "secret", units="base")


@pytest.mark.parametrize(
    "key,secret,units", [("", "secret", "base"), ("key", "", "base"), ("key", "secret", "invalid")]
)
async def test_invalid_input_never_constructs_client(monkeypatch, key, secret, units):
    def unexpected(config):
        pytest.fail("Must reject before constructing client")

    monkeypatch.setattr(account.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await account.read_balances(key, secret, units=units)


@pytest.mark.parametrize("units", ["base", "rebased"])
def test_cli_balance_evidence(monkeypatch, units):
    import json

    read = AsyncMock(return_value={"AAPL.T": "1.000"})
    monkeypatch.setattr(account, "read_balances", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(cli.app, ["run", "xstocks-balances", "--units", units])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["rebase_multiplier"] == units
    assert body["balances"] == {"AAPL.T": "1.000"}
    assert body["wallet"] == "default"
    assert body["status"] == "balances_read"
    assert body["started_at"] <= body["completed_at"]
    read.assert_awaited_once_with("key", "s", units=units)


@pytest.mark.parametrize("options", [[], ["--units", "wrong"], ["--units", "base"]])
def test_cli_rejects_missing_units_invalid_units_and_credentials(monkeypatch, options):
    read = AsyncMock()
    monkeypatch.setattr(account, "read_balances", read)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(kraken_api_key="", kraken_api_secret=""))
    result = CliRunner().invoke(cli.app, ["run", "xstocks-balances", *options])
    assert result.exit_code == 2
    read.assert_not_awaited()


def test_cli_read_failure_emits_no_success_json(monkeypatch):
    monkeypatch.setattr(
        account, "read_balances", AsyncMock(side_effect=ValueError("Kraken rejected request"))
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="key", kraken_api_secret="s"),  # noqa: S106
    )
    result = CliRunner().invoke(cli.app, ["run", "xstocks-balances", "--units", "base"])
    assert result.exit_code == 1
    assert "Balance read failed" in result.output
    assert "balances_read" not in result.output
