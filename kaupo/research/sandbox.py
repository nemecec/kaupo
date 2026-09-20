"""Run one candidate in an offline container against a fresh disposable database."""

import argparse
import asyncio
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Date, DateTime, insert, select

from kaupo.api.schemas import BacktestIn
from kaupo.backtest.plan import build_backtest_request, lint_and_load_strategies
from kaupo.backtest.portfolio import PortfolioBacktestRequest, run_portfolio_backtest
from kaupo.backtest.run import run_backtest
from kaupo.core.provenance import engine_version
from kaupo.db.models import Base, EquitySnapshotRow, EventRow, FillRow, OrderRow, RunRow
from kaupo.db.session import get_engine, get_sessionmaker, sm_scope
from kaupo.research.contracts import ExperimentIn
from kaupo.research.dataset import MARKET_TABLES, MAX_BYTES, json_value


def read_dataset(path: Path) -> tuple[dict[str, Any], str]:
    compressed = path.read_bytes()
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("dataset exceeds 64 MB")
    data = json.loads(raw)
    if data["schema"] != 1 or set(data["tables"]) != set(MARKET_TABLES):
        raise ValueError("unsupported dataset tables")
    ExperimentIn.model_validate(data["manifest"])
    if data["economics"].get("engine_version") != engine_version():
        raise ValueError("dataset and sandbox engine versions differ")
    for name, rows in data["tables"].items():
        columns = set(MARKET_TABLES[name].__table__.columns.keys())
        if any(set(row) != columns for row in rows):
            raise ValueError(f"dataset schema mismatch in {name}")
    return data, hashlib.sha256(compressed).hexdigest()


async def initialize(data: dict[str, Any]) -> None:
    # This entrypoint runs without candidate code mounted. The workflow creates
    # a new database container for EACH variant, so candidates cannot share state.
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm_scope(get_sessionmaker()) as session:
        for name, model in MARKET_TABLES.items():
            for offset in range(0, len(data["tables"][name]), 500):
                batch = []
                for original in data["tables"][name][offset : offset + 500]:
                    row = dict(original)
                    for col in model.__table__.columns:
                        if row.get(col.name) is not None:
                            if isinstance(col.type, DateTime):
                                row[col.name] = datetime.fromisoformat(row[col.name])
                            elif isinstance(col.type, Date):
                                row[col.name] = date.fromisoformat(row[col.name])
                    batch.append(row)
                await session.execute(insert(model), batch)


async def execute(data: dict[str, Any], digest: str, variant_id: str) -> dict[str, Any]:
    spec = ExperimentIn.model_validate(data["manifest"])
    variant = next(v for v in spec.variants if v.id == variant_id)
    strategies = lint_and_load_strategies(Path("/strategies"))
    body = BacktestIn(
        strategy=variant.strategy,
        params=variant.params,
        pair=spec.pairs[0] if len(spec.pairs) == 1 else None,
        pairs=spec.pairs if len(spec.pairs) > 1 else None,
        timeframe=spec.timeframe,
        start=spec.start,
        end=spec.end,
        starting_cash=spec.starting_cash,
        exchange=spec.exchange,
        marketable_limit="skip",
    )
    economics = data["economics"]
    request = replace(
        build_backtest_request(body, strategies),
        maker_fee_bps=economics["maker_bps"],
        taker_fee_bps=economics["taker_bps"],
        slippage_bps=economics["slippage_bps"],
    )
    if isinstance(request, PortfolioBacktestRequest):
        run_id, _, metrics = await run_portfolio_backtest(request, get_sessionmaker())
    else:
        run_id, _, metrics = await run_backtest(request, get_sessionmaker())
    evidence: dict[str, Any] = {}
    async with sm_scope(get_sessionmaker()) as session:
        run = await session.get(RunRow, str(run_id))
        assert run is not None
        for model in (EquitySnapshotRow, FillRow, OrderRow):
            rows = await session.execute(select(model.__table__).where(model.run_id == str(run_id)))
            evidence[model.__tablename__] = [
                {key: json_value(value) for key, value in row.items()} for row in rows.mappings()
            ]
        events = await session.execute(
            select(EventRow.__table__).where(EventRow.data["run_id"].as_string() == str(run_id))
        )
        evidence["events"] = [{k: json_value(v) for k, v in row.items()} for row in events.mappings()]
        return {
            "id": variant_id,
            "status": "completed",
            "dataset_sha256": digest,
            "strategy_ref": spec.strategy_ref,
            "run_id": str(run_id),
            "run_status": run.status,
            "config": run.config,
            "metrics": metrics,
            "evidence": evidence,
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("initialize", "run"))
    parser.add_argument("--variant")
    args = parser.parse_args()
    data, digest = read_dataset(Path("/input/dataset.json.gz"))
    if args.action == "initialize":
        await initialize(data)
        return
    try:
        result = await execute(data, digest, args.variant)
    except Exception as exc:
        result = {"id": args.variant, "status": "failed", "error": str(exc)[:4000]}
    await asyncio.to_thread(Path("/output/result.json").write_text, json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    asyncio.run(main())
