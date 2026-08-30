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
from kaupo.core.funding import StaticFundingProvider
from kaupo.core.orderflow import DbOrderFlowProvider
from kaupo.core.positioning import StaticFuturesMetricsProvider, StaticOpenInterestProvider
from kaupo.core.recorder import CompositeRecorder, DbRecorder, InMemoryRecorder, RunInfo
from kaupo.data.candles import get_candles
from kaupo.data.funding import FUNDING_EXCHANGE, get_funding_rates
from kaupo.data.futures_metrics import METRICS_EXCHANGE, get_futures_metrics_daily
from kaupo.data.open_interest import OI_EXCHANGE, get_open_interest
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
    # stability-window marker for the run config: {"group", "window", "of"}
    stability: dict[str, Any] | None = None
    # sweep marker for the run config: {"group", "point"}
    sweep: dict[str, Any] | None = None
    # rolling-origin report marker for the run config: {"period", "assignment"}
    rolling_origin: dict[str, Any] | None = None


def backtest_risk_config(
    *,
    max_position_quote: float | None = None,
    max_gross_exposure_quote: float | None = None,
    max_daily_loss_quote: float | None = None,
) -> RiskConfig:
    """Risk config for a backtest request: research overrides over the live
    defaults. None keeps the default. Backtest-only — live/shadow guardrails
    never go through here."""
    given = (
        ("max_position_quote", max_position_quote),
        ("max_gross_exposure_quote", max_gross_exposure_quote),
        ("max_daily_loss_quote", max_daily_loss_quote),
    )
    for name, value in given:
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    risk = RiskConfig()
    if max_position_quote is not None:
        risk = replace(risk, max_position_quote=max_position_quote)
    if max_gross_exposure_quote is not None:
        risk = replace(risk, max_gross_exposure_quote=max_gross_exposure_quote)
    if max_daily_loss_quote is not None:
        risk = replace(risk, max_daily_loss_quote=max_daily_loss_quote)
    return risk


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
        # funding (Binance perp of the base asset) prefilled over the same
        # window; served point-in-time from memory for determinism
        funding_rates = await get_funding_rates(
            session, FUNDING_EXCHANGE, request.pair.base, prefill_start, request.end
        )
        # positioning series of the same perp, prefilled the same way
        oi_rows = await get_open_interest(session, OI_EXCHANGE, request.pair.base, prefill_start, request.end)
        metrics_rows = await get_futures_metrics_daily(
            session, METRICS_EXCHANGE, request.pair.base, prefill_start.date(), request.end.date()
        )
    in_range = [c for c in candles if c.ts >= request.start]
    if not in_range:
        raise ValueError(
            f"No {request.exchange} candles for {request.pair} {request.timeframe.value} in range; "
            "run `kaupo ingest candles` first"
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
        funding=StaticFundingProvider({request.pair.base: funding_rates}),
        open_interest=StaticOpenInterestProvider({request.pair.base: oi_rows}),
        futures_metrics=StaticFuturesMetricsProvider({request.pair.base: metrics_rows}),
        # ticks/book are too voluminous to prefill like funding: the DB
        # provider queries per candle (rows beyond tick retention are simply
        # absent — the strategy sees empty series)
        orderflow=DbOrderFlowProvider(sessionmaker, exchange=request.exchange),
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
                **({"stability": request.stability} if request.stability is not None else {}),
                **({"sweep": request.sweep} if request.sweep is not None else {}),
                **({"rolling_origin": request.rolling_origin} if request.rolling_origin is not None else {}),
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
