"""Portfolio backtest runner: multi-pair universes over one shared quote.

Mirrors ``run_backtest``: per-pair candle prefill before ``start`` (so the
strategy sees the context a live run would), timestamp-joined steps through
the PortfolioEngine, metrics, persistence. One quote currency, one
timeframe, and one exchange across the universe — validated at request
construction, so no FX conversion ever enters the loop.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.backtest.metrics import compute_metrics
from kaupo.core.engine import RunResult
from kaupo.core.funding import StaticFundingProvider
from kaupo.core.orderflow import DbOrderFlowProvider
from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
from kaupo.core.positioning import StaticFuturesMetricsProvider, StaticOpenInterestProvider
from kaupo.core.recorder import CompositeRecorder, DbRecorder, InMemoryRecorder, RunInfo
from kaupo.data.candles import get_candles
from kaupo.data.funding import FUNDING_EXCHANGE, get_funding_rates
from kaupo.data.futures_metrics import METRICS_EXCHANGE, get_futures_metrics_daily
from kaupo.data.open_interest import OI_EXCHANGE, get_open_interest
from kaupo.db.models import RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import (
    Candle,
    FundingRate,
    FuturesMetricsDaily,
    OpenInterest,
    Pair,
    RunId,
    RunMode,
    Timeframe,
)
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy, PortfolioStrategyBase
from kaupo.venues.paper import PaperVenue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioBacktestRequest:
    strategy: LoadedStrategy
    params: dict[str, Any]
    pairs: list[Pair]
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

    def __post_init__(self) -> None:
        if len(self.pairs) < 2:
            raise ValueError("A portfolio backtest needs at least 2 pairs; use BacktestRequest for one pair")
        unique = sorted(set(self.pairs), key=str)
        if len(unique) != len(self.pairs):
            raise ValueError(f"Duplicate pairs in universe: {sorted(str(p) for p in self.pairs)}")
        quotes = {pair.quote for pair in unique}
        if len(quotes) != 1:
            raise ValueError(
                f"All pairs of a portfolio run must share one quote currency, got {sorted(quotes)}"
            )
        # canonical order: sorted by pair string, so every downstream
        # iteration (venue stepping, recording, metrics) is deterministic
        object.__setattr__(self, "pairs", unique)


async def _aiter_steps(
    steps: list[tuple[datetime, dict[Pair, Candle]]],
) -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
    for step in steps:
        yield step


async def run_portfolio_backtest(
    request: PortfolioBacktestRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[RunId, RunResult, dict[str, Any]]:
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, PortfolioStrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is not a portfolio strategy; run it with run_backtest (--pair)"
        )

    # prefill history before `start` per pair, exactly like the single-pair
    # runner — this is what keeps backtest context ≡ live context
    prefill_start = request.start - timedelta(seconds=request.timeframe.seconds * request.lookback)
    candles_by_pair: dict[Pair, list[Candle]] = {}
    funding_by_base: dict[str, list[FundingRate]] = {}
    oi_by_base: dict[str, list[OpenInterest]] = {}
    metrics_by_base: dict[str, list[FuturesMetricsDaily]] = {}
    async with sm_scope(sessionmaker) as session:
        for pair in request.pairs:
            candles_by_pair[pair] = await get_candles(
                session, pair, request.timeframe, prefill_start, request.end, exchange=request.exchange
            )
        # funding and positioning series (Binance perp per base asset)
        # prefilled over the same window; served point-in-time from memory
        # for determinism
        for base in sorted({pair.base for pair in request.pairs}):
            funding_by_base[base] = await get_funding_rates(
                session, FUNDING_EXCHANGE, base, prefill_start, request.end
            )
            oi_by_base[base] = await get_open_interest(session, OI_EXCHANGE, base, prefill_start, request.end)
            metrics_by_base[base] = await get_futures_metrics_daily(
                session, METRICS_EXCHANGE, base, prefill_start.date(), request.end.date()
            )
    for pair, candles in candles_by_pair.items():
        if not any(c.ts >= request.start for c in candles):
            raise ValueError(
                f"No {request.exchange} candles for {pair} {request.timeframe.value} in range; "
                "run `kaupo ingest candles` first"
            )

    steps = list(joined_steps(candles_by_pair))
    warmup = sum(1 for ts, _ in steps if ts < request.start)
    log.info(
        "Backtesting %s on %d universe steps (+%d warm-up) over %s",
        request.strategy.id,
        len(steps) - warmup,
        warmup,
        ", ".join(str(p) for p in request.pairs),
    )

    memory = InMemoryRecorder()
    recorder: CompositeRecorder | InMemoryRecorder = (
        CompositeRecorder([DbRecorder(sessionmaker), memory]) if request.persist else memory
    )

    # keep risk's cost model in sync with the venue
    risk_config = replace(
        request.risk, taker_fee_bps=request.taker_fee_bps, slippage_bps=request.slippage_bps
    )
    risk = RiskManager(risk_config)
    universe = [str(pair) for pair in request.pairs]
    engine = PortfolioEngine(
        strategy=strategy,
        venues={
            pair: PaperVenue(request.taker_fee_bps, request.maker_fee_bps, request.slippage_bps)
            for pair in request.pairs
        },
        risk=risk,
        ledger=Ledger(request.pairs[0].quote, request.starting_cash, request.start),
        recorder=recorder,
        config=PortfolioEngineConfig(
            pairs=tuple(request.pairs),
            timeframe=request.timeframe,
            lookback=request.lookback,
            liquidate_end=request.liquidate_end,
        ),
        funding=StaticFundingProvider(funding_by_base),
        open_interest=StaticOpenInterestProvider(oi_by_base),
        futures_metrics=StaticFuturesMetricsProvider(metrics_by_base),
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
                # the joined sorted list keeps the config["pair"] shape a
                # plain string, as in single-pair runs
                "pair": ",".join(universe),
                "pairs": universe,
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

    result = await engine.run(_aiter_steps(steps), warmup=warmup)

    metrics = compute_metrics(
        equity=[(ts, float(eq)) for ts, eq, _, _ in memory.equity],
        fills=memory.fills,
        timeframe=request.timeframe,
        starting_cash=request.starting_cash,
        risk_rejections=len(risk.rejections),
        universe=universe,
    )
    metrics["status"] = result.status.value
    if result.halt_reason:
        metrics["halt_reason"] = result.halt_reason

    if request.persist:
        async with sm_scope(sessionmaker) as session:
            await session.execute(update(RunRow).where(RunRow.id == recorder.run_id).values(metrics=metrics))
    return recorder.run_id, result, metrics
