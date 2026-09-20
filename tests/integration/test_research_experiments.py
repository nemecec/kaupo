"""Preregistration survives failures and exports only immutable public data."""

import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from kaupo.config import get_settings
from kaupo.db.models import CandleRow, ResearchExperimentRow

pytestmark = pytest.mark.integration
AUTH = {"Authorization": "Bearer research-test"}


@pytest.fixture
async def client(session, monkeypatch):
    monkeypatch.setenv("KAUPO_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("KAUPO_READONLY_TOKEN", "readonly-test")
    monkeypatch.setenv("KAUPO_RESEARCH_TOKEN", "research-test")
    monkeypatch.setenv("KAUPO_RESEARCH_IMAGE", "ghcr.io/nemecec/kaupo:" + "a" * 40)
    get_settings.cache_clear()
    from kaupo.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    get_settings.cache_clear()


def contract():
    return {
        "reference": "test-candidate-1",
        "strategy_ref": "a" * 40,
        "mandate": "trend",
        "hypothesis": "A fixed entry delay reduces losses after actual trading fees",
        "changed_factor": "Entry delay of exactly one bar",
        "rejection_rule": "Reject if net return does not improve",
        "variants": [
            {"id": "control", "strategy": "sma-cross"},
            {"id": "delay", "strategy": "sma-cross", "params": {"fast": 12}},
        ],
        "exchange": "kraken",
        "pairs": ["SOL/EUR"],
        "timeframe": "1d",
        "start": "2025-01-01T00:00:00Z",
        "end": "2025-01-03T00:00:00Z",
    }


async def register(client):
    response = await client.post("/api/v1/research/experiments", headers=AUTH, json=contract())
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_contract_immutable_and_every_failure_recorded(client):
    eid = await register(client)
    assert await register(client) == eid
    changed = contract()
    changed["hypothesis"] = "Another hypothesis cannot replace this registered contract"
    assert (await client.post("/api/v1/research/experiments", headers=AUTH, json=changed)).status_code == 409
    payload = {
        "dataset_sha256": "0" * 64,
        "workflow_run_id": 1,
        "outcomes": [{"id": "control", "status": "failed"}, {"id": "control", "status": "failed"}],
    }
    path = f"/api/v1/research/experiments/{eid}/result"
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 422
    payload["outcomes"][1] = {"id": "delay", "status": "not_run"}
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 200
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 200
    payload["outcomes"][1]["status"] = "completed"
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 409
    row = (await client.get(f"/api/v1/research/experiments/{eid}", headers=AUTH)).json()
    assert row["status"] == "reported"
    assert row["automatic_live_promotion"] is False
    listing = (await client.get("/api/v1/research/experiments", headers=AUTH)).json()
    assert listing["registered_variants_total"] == 2
    assert listing["registered_experiments_total"] == 1


async def test_dataset_frozen_scoped_and_success_requires_digest(client, session):
    for day in (1, 2, 3):
        session.add(
            CandleRow(
                exchange="kraken",
                pair="SOL/EUR",
                timeframe="1d",
                ts=datetime(2025, 1, day, tzinfo=UTC),
                open=10,
                high=11,
                low=9,
                close=10,
                volume=100,
            )
        )
    await session.commit()
    eid = await register(client)
    path = f"/api/v1/research/experiments/{eid}/dataset"
    response = await client.get(path, headers=AUTH)
    assert response.status_code == 200, response.text
    data = json.loads(gzip.decompress(response.content))
    assert set(data["tables"]) == {
        "candles",
        "funding_rates",
        "futures_metrics_daily",
        "open_interest",
        "orderflow_daily",
    }
    assert len(data["tables"]["candles"]) == 2  # end is exclusive
    assert data["candle_coverage"]["SOL/EUR"]["expected_bars"] == 2
    assert hashlib.sha256(response.content).hexdigest() == response.headers["X-Dataset-SHA256"]
    candle = await session.get(CandleRow, ("kraken", "SOL/EUR", "1d", datetime(2025, 1, 1, tzinfo=UTC)))
    candle.close = 99
    await session.commit()
    assert (await client.get(path, headers=AUTH)).content == response.content
    result = {
        "dataset_sha256": "0" * 64,
        "workflow_run_id": 2,
        "outcomes": [{"id": "control", "status": "completed"}, {"id": "delay", "status": "failed"}],
    }
    report = f"/api/v1/research/experiments/{eid}/result"
    assert (await client.post(report, headers=AUTH, json=result)).status_code == 409
    result["dataset_sha256"] = response.headers["X-Dataset-SHA256"]
    assert (await client.post(report, headers=AUTH, json=result)).status_code == 200


async def test_empty_data_and_readonly_registration_refused(client, session):
    response = await client.post(
        "/api/v1/research/experiments", headers={"Authorization": "Bearer readonly-test"}, json=contract()
    )
    assert response.status_code == 403
    eid = await register(client)
    assert (await client.get(f"/api/v1/research/experiments/{eid}/dataset", headers=AUTH)).status_code == 422
    row = await session.get(ResearchExperimentRow, eid)
    assert row.dataset is None
    assert row.status == "registered"


async def test_offline_runner_loads_snapshot_and_preserves_full_evidence(session, monkeypatch, tmp_path):
    """Exercise the same loader and runner used by cloud containers, without an API token."""
    from datetime import timedelta

    from kaupo.core.provenance import engine_version
    from kaupo.research import sandbox
    from kaupo.research.dataset import MARKET_TABLES
    from kaupo.sdk.loader import load_strategies

    manifest = contract()
    manifest["end"] = "2025-05-01T00:00:00Z"
    manifest["variants"][0]["strategy"] = "buy-and-sell"
    candles = []
    for offset in range(120):
        price = 10 + offset / 10
        candles.append(
            {
                "exchange": "kraken",
                "pair": "SOL/EUR",
                "timeframe": "1d",
                "ts": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=offset)).isoformat(),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 100,
            }
        )
    data = {
        "schema": 1,
        "manifest": manifest,
        "economics": {
            "maker_bps": 40,
            "taker_bps": 80,
            "slippage_bps": 5,
            "engine_version": engine_version(),
        },
        "tables": {name: candles if name == "candles" else [] for name in MARKET_TABLES},
    }
    path = tmp_path / "dataset.json.gz"
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    loaded, digest = sandbox.read_dataset(path)
    await sandbox.initialize(loaded)
    from tests.integration.test_backtest_db import STRATEGY

    (tmp_path / "strategy.py").write_text(STRATEGY)
    strategies = load_strategies(tmp_path)
    monkeypatch.setattr(sandbox, "lint_and_load_strategies", lambda _: strategies)
    result = await sandbox.execute(loaded, digest, "control")
    assert result["status"] == "completed"
    assert result["dataset_sha256"] == digest
    assert result["config"]["fees"]["taker_bps"] == 80
    assert len(result["evidence"]["equity_snapshots"]) >= 120
    assert set(result["evidence"]) == {"equity_snapshots", "fills", "orders", "events"}
    assert len(result["evidence"]["fills"]) == 2
