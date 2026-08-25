"""Backtest runner: wires components, feeds historical candles, computes metrics."""

import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.metrics import compute_metrics
from kaupo.core.engine import Engine, EngineConfig, RunResult
from kaupo.core.recorder import CompositeRecorder, DbRecorder, InMemoryRecorder, RunInfo
from kaupo.data.candles import get_candles
from kaupo.db.models import RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Candle, Pair, RunId, RunMode, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy, StrategyBase
from kaupo.venues.paper import PaperVenue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestRequest:
    strategy: LoadedStrategy
    params: dict[str, Any]
    pair: Pair
    timeframe: Timeframe
    start: datetime
    end: datetime
    starting_cash: float = 10_000.0
    taker_fee_bps: float = 26.0
    maker_fee_bps: float = 16.0
    slippage_bps: float = 5.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    lookback: int = 300
    liquidate_end: bool = True
    persist: bool = True
    exchange: str = "kraken"  # which exchange's stored candles to run on


async def _aiter(candles: list[Candle]) -> AsyncIterator[Candle]:
    for candle in candles:
        yield candle


async def run_backtest(
    request: BacktestRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[RunId, RunResult, dict[str, Any]]:
    # prefill history before `start` so the strategy sees the same context a
    # live/shadow run would — this is what makes backtest ≡ shadow
    prefill_start = request.start - timedelta(seconds=request.timeframe.seconds * request.lookback)
    async with sm_scope(sessionmaker) as session:
        candles = await get_candles(
            session, request.pair, request.timeframe, prefill_start, request.end, exchange=request.exchange
        )
    in_range = [c for c in candles if c.ts >= request.start]
    if not in_range:
        raise ValueError(
            f"No {request.exchange} candles for {request.pair} {request.timeframe.value} in range; "
            "run `kaupo ingest` first"
        )
    warmup = len(candles) - len(in_range)
    log.info("Backtesting %s on %d candles (+%d warm-up)", request.strategy.id, len(in_range), warmup)

    memory = InMemoryRecorder()
    recorder: CompositeRecorder | InMemoryRecorder = (
        CompositeRecorder([DbRecorder(sessionmaker), memory]) if request.persist else memory
    )

    # keep risk's cost model in sync with the venue
    risk_config = replace(
        request.risk, taker_fee_bps=request.taker_fee_bps, slippage_bps=request.slippage_bps
    )
    risk = RiskManager(risk_config)
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, StrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is a portfolio strategy; use run_portfolio_backtest (--pairs)"
        )
    engine = Engine(
        strategy=strategy,
        venue=PaperVenue(request.taker_fee_bps, request.maker_fee_bps, request.slippage_bps),
        risk=risk,
        ledger=Ledger(request.pair.quote, request.starting_cash, request.start),
        recorder=recorder,
        config=EngineConfig(
            pair=request.pair,
            timeframe=request.timeframe,
            lookback=request.lookback,
            liquidate_end=request.liquidate_end,
        ),
        run_info=RunInfo(
            mode=RunMode.BACKTEST,
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            strategy_source_hash=request.strategy.source_hash,
            config={
                "pair": str(request.pair),
                "timeframe": request.timeframe.value,
                "exchange": request.exchange,
                "params": request.params,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "starting_cash": request.starting_cash,
                "fees": {
                    "taker_bps": request.taker_fee_bps,
                    "maker_bps": request.maker_fee_bps,
                    "slippage_bps": request.slippage_bps,
                },
                "risk": asdict(request.risk),
                "lookback": request.lookback,
                "liquidate_end": request.liquidate_end,
            },
        ),
    )

    result = await engine.run(_aiter(candles), warmup=warmup)

    metrics = compute_metrics(
        equity=[(ts, float(eq)) for ts, eq, _, _ in memory.equity],
        fills=memory.fills,
        timeframe=request.timeframe,
        starting_cash=request.starting_cash,
        risk_rejections=len(risk.rejections),
    )
    metrics["status"] = result.status.value
    if result.halt_reason:
        metrics["halt_reason"] = result.halt_reason

    if request.persist:
        async with sm_scope(sessionmaker) as session:
            await session.execute(update(RunRow).where(RunRow.id == recorder.run_id).values(metrics=metrics))
    return recorder.run_id, result, metrics
