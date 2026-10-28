from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.data.xstocks import XStocksClient
from kaupo.data.xstocks_identity import identity_maps, map_account_identities
from kaupo.venues import xstocks_fills, xstocks_ledgers

ROW = {
    "aclass_base": "tokenized_asset",
    "aclass_quote": "currency",
    "execution_venue": "international",
    "altname": "AAPLxUSD",
    "wsname": "AAPLx/USD",
    "base": "AAPLx",
    "quote": "ZUSD",
}
ROWS = {"AAPLSPVUSD": ROW}


def test_exact_aliases_and_assets_only():
    pairs, assets, classes = identity_maps({**ROWS, "AAPLx/USD": deepcopy(ROW)})
    assert pairs == {"AAPLSPVUSD": "AAPLX/USD", "AAPLxUSD": "AAPLX/USD", "AAPLx/USD": "AAPLX/USD"}
    assert assets == {"AAPLx": "AAPLX", "ZUSD": "USD"}
    assert classes == {"AAPLx": "tokenized_asset", "ZUSD": "currency"}
    assert "AAPLXUSD" not in pairs
    assert "AAPL" not in assets
    assert "AAPL.T" not in assets
    assert "USD.F" not in assets


def test_mapping_keeps_unknowns_and_input_unchanged():
    fills = {"a": {"pair": "AAPLSPVUSD"}, "b": {"pair": "XBTUSD"}, "c": {"pair": "AAPLXUSD"}}
    ledgers = {
        "x": {"asset": "AAPLx", "aclass": "tokenized_asset"},
        "y": {"asset": "ZUSD", "aclass": "currency"},
        "z": {"asset": "USD.F"},
    }
    before = deepcopy((fills, ledgers))
    assert map_account_identities(ROWS, fills, ledgers) == {
        "fill_pairs": {"a": "AAPLX/USD"},
        "ledger_assets": {"x": "AAPLX", "y": "USD"},
        "unmapped_pairs": ["AAPLXUSD", "XBTUSD"],
        "unmapped_assets": ["USD.F"],
        "ledger_class_mismatches": {},
    }
    assert (fills, ledgers) == before
    assert map_account_identities({}, {}, None) == {
        "fill_pairs": {},
        "ledger_assets": {},
        "unmapped_pairs": [],
        "unmapped_assets": [],
        "ledger_class_mismatches": {},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("base", ""),
        ("quote", None),
        ("wsname", "bad"),
        ("wsname", "USD/USD"),
        ("altname", 1),
        ("base", "AAPL T"),
    ],
)
def test_missing_or_malformed_metadata_fails(field, value):
    with pytest.raises(ValueError):
        identity_maps({"key": {**ROW, field: value}})


@pytest.mark.parametrize(
    "rows",
    [
        {"key": None},
        {"": ROW},
        {**ROWS, "other": {**ROW, "altname": "AAPLxUSD", "wsname": "OTHER/USD", "base": "OTHER.T"}},
        {**ROWS, "other": {**ROW, "altname": "OTHERUSD", "wsname": "OTHER/USD"}},
    ],
)
def test_conflicting_or_invalid_identity_fails(rows):
    with pytest.raises(ValueError):
        identity_maps(rows)


def test_other_classes_and_venues_are_excluded():
    assert identity_maps(
        {
            "crypto": {"aclass_base": "currency"},
            "other": {
                "aclass_base": "tokenized_asset",
                "aclass_quote": "currency",
                "execution_venue": "other",
            },
        }
    ) == ({}, {}, {})


async def test_public_metadata_request_preserves_internal_ids():
    def handler(request):
        assert request.url.path == "/0/public/AssetPairs"
        assert dict(request.url.params) == {
            "aclass_base": "tokenized_asset",
            "execution_venue": "international",
        }
        return httpx.Response(200, json={"error": [], "result": ROWS})

    async with XStocksClient(transport=httpx.MockTransport(handler), request_interval=0) as client:
        assert await client.identity_metadata() == ROWS


async def test_public_transport_failure_is_controlled():
    def handler(request):
        raise httpx.ConnectError("network detail")

    async with XStocksClient(transport=httpx.MockTransport(handler), request_interval=0) as client:
        with pytest.raises(ValueError, match=r"^Kraken identity metadata request failed \(ConnectError\)$"):
            await client.identity_metadata()


@pytest.mark.parametrize(
    "map_assets,include_ledgers,fail",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
def test_cli_optional_mapping(monkeypatch, map_assets, include_ledgers, fail):
    import json

    fills = {"f": {"pair": "AAPLxUSD"}}
    ledger_entries = {"l": {"asset": "AAPLx", "aclass": "tokenized_asset"}}
    monkeypatch.setattr(xstocks_fills, "read_fills", AsyncMock(return_value=fills))
    monkeypatch.setattr(xstocks_ledgers, "read_fill_ledgers", AsyncMock(return_value=ledger_entries))
    metadata = AsyncMock(return_value=ROWS, side_effect=ValueError("Bad metadata") if fail else None)
    monkeypatch.setattr(XStocksClient, "identity_metadata", metadata)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    args = ["run", "xstocks-fills", "--units", "base", "--start", "100", "--end", "200"]
    if map_assets:
        args.append("--map-assets")
    if include_ledgers:
        args.append("--include-ledgers")
    result = CliRunner().invoke(cli.app, args)
    if fail:
        assert result.exit_code == 1
        assert "fills_and_ledgers_read" not in result.output
    else:
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert body["fills"] == fills
        if map_assets:
            assert body["identities"]["fill_pairs"] == {"f": "AAPLX/USD"}
            assert body["identities"]["ledger_assets"] == ({"l": "AAPLX"} if include_ledgers else {})
        else:
            assert "identities" not in body
    assert metadata.await_count == int(map_assets)


@pytest.mark.parametrize(
    "asset,aclass,expected",
    [
        ("AAPLx", "currency", "tokenized_asset"),
        ("AAPLx", None, "tokenized_asset"),
        ("ZUSD", "tokenized_asset", "currency"),
    ],
)
def test_known_id_with_wrong_or_missing_class_stays_unmapped(asset, aclass, expected):
    report = map_account_identities(ROWS, {}, {"l": {"asset": asset, "aclass": aclass}})
    assert report["ledger_assets"] == {}
    assert report["unmapped_assets"] == [asset]
    assert report["ledger_class_mismatches"] == {
        "l": {"asset": asset, "expected_class": expected, "actual_class": aclass}
    }


@pytest.mark.parametrize("value", [None, "", "bad class", 1])
def test_missing_or_invalid_quote_class_is_not_assumed(value):
    with pytest.raises(ValueError, match="Missing Kraken pair or asset identity"):
        identity_maps({"key": {**ROW, "aclass_quote": value}})


def test_same_raw_asset_with_conflicting_classes_rejects_metadata():
    with pytest.raises(ValueError, match="Conflicting"):
        identity_maps({**ROWS, "other": {**ROW, "aclass_quote": "tokenized_asset"}})
