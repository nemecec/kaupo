"""Export only bounded public market observations, never trading/account tables."""

import gzip
import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.db.models import (
    CandleRow,
    FundingRateRow,
    FuturesMetricsDailyRow,
    OpenInterestRow,
    OrderflowDailyRow,
    ResearchExperimentRow,
)
from kaupo.domain import Timeframe
from kaupo.research.contracts import ExperimentIn

MARKET_TABLES = {
    row.__tablename__: row
    for row in (CandleRow, FundingRateRow, FuturesMetricsDailyRow, OpenInterestRow, OrderflowDailyRow)
}
MAX_ROWS = 250000
MAX_BYTES = 64 * 1024 * 1024


def json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


async def snapshot(session: AsyncSession, experiment: ResearchExperimentRow) -> tuple[bytes, str]:
    spec = ExperimentIn.model_validate(experiment.manifest)
    bases = [p.split("/")[0] for p in spec.pairs]
    # 400 days covers the fixed 300-bar engine warmup and the daily feature windows.
    start, end = spec.start - timedelta(days=400), spec.end
    rows: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for name, model in MARKET_TABLES.items():
        table = model.__table__
        conditions = []
        if name == "candles":
            conditions = [
                table.c.exchange == spec.exchange,
                table.c.pair.in_(spec.pairs),
                table.c.timeframe == spec.timeframe,
            ]
        elif "base_asset" in table.c:
            conditions = [table.c.exchange == "binance", table.c.base_asset.in_(bases)]
        else:
            conditions = [table.c.exchange == spec.exchange, table.c.pair.in_(spec.pairs)]
        time_col = table.c.day if "day" in table.c else table.c.ts
        lower, upper = (start.date(), end.date()) if "day" in table.c else (start, end)
        result = await session.execute(
            select(table)
            .where(*conditions, time_col >= lower, time_col < upper)
            .order_by(*table.primary_key)
            .limit(MAX_ROWS - total + 1)
        )
        data = [{k: json_value(v) for k, v in r.items()} for r in result.mappings()]
        total += len(data)
        if total > MAX_ROWS:
            raise ValueError("dataset exceeds 250000 rows; reduce the window or universe")
        rows[name] = data
    if not rows["candles"]:
        raise ValueError("no candles available for the declared experiment")
    coverage = {}
    for pair in spec.pairs:
        times = sorted(
            r["ts"]
            for r in rows["candles"]
            if r["pair"] == pair and datetime.fromisoformat(r["ts"]) >= spec.start
        )
        if not times:
            raise ValueError(f"no in-window candles for {pair}")
        coverage[pair] = {
            "bars": len(times),
            "first": times[0],
            "last": times[-1],
            "expected_bars": int(
                (spec.end - spec.start).total_seconds() / Timeframe.parse(spec.timeframe).seconds
            ),
        }
    payload = {
        "candle_coverage": coverage,
        "schema": 1,
        "experiment_id": experiment.id,
        "manifest": experiment.manifest,
        "economics": experiment.economics,
        "tables": rows,
        "omitted_features": ["raw trade ticks", "intraday order book"],
        "limitations": [
            "Historical research data; not untouched forward evidence.",
            "Missing historical feature rows remain missing, never fabricated.",
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("dataset exceeds 64 MB; reduce the window or universe")
    compressed = gzip.compress(raw, compresslevel=1, mtime=0)
    return compressed, hashlib.sha256(compressed).hexdigest()
