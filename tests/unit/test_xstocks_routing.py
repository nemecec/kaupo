"""Explicit routing preserves crypto defaults and isolates xStocks run identity."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from kaupo.api.routes.assignments import _validate_live
from kaupo.core import supervisor as sup
from kaupo.core.resume import config_hash, run_config_hash
from kaupo.db.models import RunRow
from tests.unit.test_assignment_fees import assignment


def test_asset_class_changes_resume_identity_and_restart_hash():
    base = assignment(pair="AAPLX/USD")
    token = replace(base, asset_class="tokenized_asset")
    legacy = RunRow(strategy_id="s", config={"pair": base.pair, "timeframe": "1h", "params": {}})
    original = run_config_hash(legacy)
    assert original == config_hash("s", base.pair, "1h", {}, asset_class="currency")
    legacy.config = {**legacy.config, "asset_class": "tokenized_asset"}
    assert run_config_hash(legacy) != original
    assert run_config_hash(legacy) == config_hash("s", base.pair, "1h", {}, asset_class="tokenized_asset")
    assert sup.assignment_config_hash(base) != sup.assignment_config_hash(token)


@pytest.mark.parametrize("portfolio", [False, True])
async def test_supervisor_uses_dedicated_client_without_funding(monkeypatch, portfolio):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    def forbidden():
        pytest.fail("crypto client constructed for xStocks")

    monkeypatch.setattr(sup, "XStocksClient", Client)
    monkeypatch.setattr(sup, "KrakenClient", forbidden)
    monkeypatch.setattr(sup, "BinanceClient", forbidden)
    runner = AsyncMock(return_value="done")
    monkeypatch.setattr(sup, "run_portfolio_shadow" if portfolio else "run_shadow", runner)
    row = assignment(
        asset_class="tokenized_asset",
        pair="AAPLX/USD",
        pairs=["AAPLX/USD", "SPYX/USD"] if portfolio else None,
    )
    await sup._run_assignment(row, {"s": object()}, None, asyncio.Event(), 20, 1800)
    assert runner.call_args.args[0].asset_class == "tokenized_asset"
    assert isinstance(runner.call_args.args[2], Client)
    assert runner.call_args.kwargs["funding_client"] is None


async def test_live_rejected_before_clients_open(monkeypatch):
    def forbidden():
        pytest.fail("exchange client constructed for unsupported live request")

    monkeypatch.setattr(sup, "KrakenClient", forbidden)
    row = assignment(asset_class="tokenized_asset", mode="live")
    assert "not supported" in sup.live_start_error(row, None)
    with pytest.raises(sup.LiveTradingUnavailable, match="not supported"):
        await sup._run_assignment(row, {"s": object()}, None, asyncio.Event(), 20, 1800)
    with pytest.raises(HTTPException) as error:
        _validate_live("live", False, "tokenized_asset")
    assert error.value.status_code == 422
