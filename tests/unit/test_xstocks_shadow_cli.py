import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.core import runner as runners
from kaupo.data import binance, kraken, settings, xstocks
from kaupo.db import session as db

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "strategies"


@pytest.fixture
def clients(monkeypatch):
    counts = {"xstocks": 0, "kraken": 0, "binance": 0}
    closed = []

    def factory(name):
        class Client:
            def __init__(self):
                counts[name] += 1
                self.name = name

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                closed.append(name)

        return Client

    monkeypatch.setattr(xstocks, "XStocksClient", factory("xstocks"))
    monkeypatch.setattr(kraken, "KrakenClient", factory("kraken"))
    monkeypatch.setattr(binance, "BinanceClient", factory("binance"))
    monkeypatch.setattr(db, "get_sessionmaker", lambda: object())
    resolve = AsyncMock(side_effect=AssertionError("Static runs must not read shared settings"))
    monkeypatch.setattr(settings, "resolve_shadow_config", resolve)
    return counts, closed, resolve


def invoke(args):
    return CliRunner().invoke(cli.app, ["run", "shadow", "--strategies-dir", str(EXAMPLES), *args])


@pytest.mark.parametrize("portfolio", [False, True])
@pytest.mark.parametrize("asset_class", ["currency", "tokenized_asset"])
def test_source_schedule_fees_and_cleanup(monkeypatch, tmp_path, clients, portfolio, asset_class):
    counts, closed, resolve = clients
    schedule = {"timezone": "UTC", "week": {"0": [{"start": 0, "end": 1440}]}, "overrides": {}}
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(schedule))
    seen = []

    async def run(request, sm, client, stop=None, funding_client=None):
        seen.append(request)
        assert request.asset_class == asset_class
        assert request.maker_fee_bps == -2
        assert request.taker_fee_bps == 10
        assert request.schedule_provider().timezone == "UTC"
        assert client.name == ("xstocks" if asset_class == "tokenized_asset" else "kraken")
        assert (funding_client is None) == (asset_class == "tokenized_asset")
        return SimpleNamespace(
            status=SimpleNamespace(value="halted"), num_fills=0, final_equity=10000, halt_reason=None
        )

    monkeypatch.setattr(runners, "run_shadow", run)
    monkeypatch.setattr(runners, "run_portfolio_shadow", run)
    args = [
        "--no-config-from-db",
        "--asset-class",
        asset_class,
        "--timeframe",
        "1h",
        "--trading-schedule",
        str(path),
        "--maker-fee-bps",
        "-2",
        "--taker-fee-bps",
        "10",
    ]
    args += (
        ["--strategy", "momentum-rotation", "--pairs", "AAPLX/USD,TSLAX/USD"]
        if portfolio
        else ["--strategy", "regime-switch", "--pair", "AAPLX/USD"]
    )
    result = invoke(args)
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert counts == (
        {"xstocks": 1, "kraken": 0, "binance": 0}
        if asset_class == "tokenized_asset"
        else {"xstocks": 0, "kraken": 1, "binance": 1}
    )
    assert sorted(closed) == (["xstocks"] if asset_class == "tokenized_asset" else ["binance", "kraken"])
    resolve.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        ["--asset-class", "bad"],
        ["--asset-class", "tokenized_asset"],
        ["--maker-fee-bps", "nan"],
        ["--taker-fee-bps", "inf"],
    ],
)
def test_invalid_options_before_clients(clients, args):
    result = invoke(args)
    assert result.exit_code == 2, result.output
    assert not any(clients[0].values())


def test_invalid_schedule_before_clients(clients, tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text("{}")
    result = invoke(["--trading-schedule", str(path)])
    assert result.exit_code == 2
    assert not any(clients[0].values())


def test_xstocks_client_closes_on_runner_failure(monkeypatch, clients):
    monkeypatch.setattr(runners, "run_shadow", AsyncMock(side_effect=ValueError("Unknown xStocks pair")))
    result = invoke(
        [
            "--asset-class",
            "tokenized_asset",
            "--no-config-from-db",
            "--strategy",
            "regime-switch",
            "--pair",
            "AAPLX/USD",
            "--timeframe",
            "1h",
        ]
    )
    assert result.exit_code != 0
    assert clients[1] == ["xstocks"]
