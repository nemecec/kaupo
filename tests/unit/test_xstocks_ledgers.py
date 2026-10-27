from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_fills
from kaupo.venues import xstocks_ledgers as ledgers

ENTRY = {
    "refid": "fill-id",
    "type": "trade",
    "aclass": "currency",
    "asset": "ZUSD",
    "amount": "-123.4500000000001",
    "fee": "-0.02469",
    "balance": "100.00",
    "time": 150.125,
}


@pytest.mark.parametrize("units", ["base", "rebased"])
async def test_signed_batches_deduplicate_references_and_preserve_entries(monkeypatch, units):
    ids = [f"L{i}" for i in range(21)]
    fills = {"one": {"ledgers": ids}, "two": {"ledgers": [ids[0]]}}
    entries = {key: {**ENTRY, "asset": "AAPL.T" if i % 2 else "ZUSD"} for i, key in enumerate(ids)}
    exchange = ledgers.ccxt.kraken({"apiKey": "test-key", "secret": "c2VjcmV0", "enableRateLimit": False})
    fetch = AsyncMock(
        side_effect=[
            {"error": [], "result": {k: entries[k] for k in ids[:20]}},
            {"error": [], "result": {ids[-1]: entries[ids[-1]]}},
        ]
    )
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(ledgers.ccxt, "kraken", lambda config: exchange)
    assert await ledgers.read_fill_ledgers("key", "secret", units=units, fills=fills) == entries
    assert fetch.await_count == 2
    for call, batch in zip(fetch.call_args_list, [ids[:20], ids[20:]], strict=True):
        url, method, headers, body = call.args
        assert url == "https://api.kraken.com/0/private/QueryLedgers"
        assert method == "POST"
        assert headers["API-Sign"]
        params = parse_qs(body)
        assert params.pop("nonce")
        assert params == {"id": [",".join(batch)], "rebase_multiplier": [units]}


@pytest.fixture
def client(monkeypatch):
    exchange = SimpleNamespace(private_post_queryledgers=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(ledgers.ccxt, "kraken", lambda config: exchange)
    return exchange


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": ["secret"]},
        {"error": [], "result": []},
        {"error": [], "result": {}},
        {"error": [], "result": {"wrong": ENTRY}},
        {"error": [], "result": {"L": ENTRY, "extra": ENTRY}},
        {"error": [], "result": {"L": None}},
    ],
)
async def test_bad_response_fails_and_closes(client, response):
    client.private_post_queryledgers.return_value = response
    with pytest.raises(ValueError):
        await ledgers.read_fill_ledgers("key", "secret", units="base", fills={"f": {"ledgers": ["L"]}})
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("asset", ""),
        ("refid", None),
        ("aclass", 1),
        ("amount", "bad"),
        ("fee", "NaN"),
        ("balance", "Infinity"),
        ("amount", 1),
        ("time", True),
        ("time", -1),
        ("time", None),
    ],
)
async def test_invalid_entry_fails(client, field, value):
    client.private_post_queryledgers.return_value = {"error": [], "result": {"L": {**ENTRY, field: value}}}
    with pytest.raises(ValueError):
        await ledgers.read_fill_ledgers("key", "secret", units="base", fills={"f": {"ledgers": ["L"]}})


@pytest.mark.parametrize("references", [None, [], "L", [""], ["a,b"], ["a b"], [3]])
async def test_bad_references_before_client(monkeypatch, references):
    def unexpected(config):
        pytest.fail("Must reject before constructing client")

    monkeypatch.setattr(ledgers.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await ledgers.read_fill_ledgers("key", "secret", units="base", fills={"f": {"ledgers": references}})


async def test_empty_fills_and_id_bound_before_client(monkeypatch):
    def unexpected(config):
        pytest.fail("Must not construct client")

    monkeypatch.setattr(ledgers.ccxt, "kraken", unexpected)
    assert await ledgers.read_fill_ledgers("key", "secret", units="base", fills={}) == {}
    ids = [f"L{i}" for i in range(2000)]
    assert ledgers.fill_ledger_ids({"f": {"ledgers": ids}}) == ids
    with pytest.raises(ValueError, match="2000 IDs"):
        await ledgers.read_fill_ledgers(
            "key", "secret", units="base", fills={"f": {"ledgers": [*ids, "extra"]}}
        )


@pytest.mark.parametrize("key,secret,units", [("", "s", "base"), ("k", "", "base"), ("k", "s", "bad")])
async def test_invalid_inputs_before_client(monkeypatch, key, secret, units):
    def unexpected(config):
        pytest.fail("Must reject before constructing client")

    monkeypatch.setattr(ledgers.ccxt, "kraken", unexpected)
    with pytest.raises(ValueError):
        await ledgers.read_fill_ledgers(key, secret, units=units, fills={})


async def test_later_failure_discards_partial_results_and_sanitizes(client):
    ids = [f"L{i}" for i in range(21)]
    client.private_post_queryledgers.side_effect = [
        {"error": [], "result": {key: ENTRY for key in ids[:20]}},
        RuntimeError("secret signed request"),
    ]
    with pytest.raises(ValueError, match=r"^Kraken ledger request failed \(RuntimeError\)$"):
        await ledgers.read_fill_ledgers("key", "secret", units="base", fills={"f": {"ledgers": ids}})
    assert client.private_post_queryledgers.await_count == 2
    client.close.assert_awaited_once()


async def test_close_error_is_sanitized(client):
    client.private_post_queryledgers.return_value = {"error": [], "result": {"L": ENTRY}}
    client.close.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match=r"^Could not close the Kraken ledger client$"):
        await ledgers.read_fill_ledgers("key", "secret", units="base", fills={"f": {"ledgers": ["L"]}})


@pytest.mark.parametrize("include,fail", [(False, False), (True, False), (True, True)])
def test_cli_optional_ledger_evidence(monkeypatch, include, fail):
    import json

    fills = {"f": {"ledgers": ["L"]}}
    monkeypatch.setattr(xstocks_fills, "read_fills", AsyncMock(return_value=fills))
    read = AsyncMock(return_value={"L": ENTRY}, side_effect=ValueError("Missing ledger") if fail else None)
    monkeypatch.setattr(ledgers, "read_fill_ledgers", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    args = ["run", "xstocks-fills", "--units", "base", "--start", "100", "--end", "200"]
    result = CliRunner().invoke(cli.app, args + (["--include-ledgers"] if include else []))
    if fail:
        assert result.exit_code == 1
        assert "fills_and_ledgers_read" not in result.output
    else:
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert body["fills"] == fills
        if include:
            assert body["ledgers"] == {"L": ENTRY}
            assert body["status"] == "fills_and_ledgers_read"
        else:
            assert "ledgers" not in body
            assert body["status"] == "fills_read"
    if include:
        read.assert_awaited_once_with("k", "s", units="base", fills=fills)
    else:
        read.assert_not_awaited()
