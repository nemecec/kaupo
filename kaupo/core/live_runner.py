"""Live trading: the shadow loop with real money on Kraken spot.

Everything that is not execution stays the same as ``run_shadow`` — the
candle poller, the warm-up from Postgres, the engine, the ledger, the risk
manager, the recorder, the control channel. Two things differ:

- the venue is :class:`~kaupo.venues.kraken_live.KrakenVenue`, so orders
  reach the exchange and fills come back from Kraken's trade data
- the run reconciles against the exchange before its first candle
  (``kaupo/core/live_reconcile.py``): open orders are cancelled, trades the
  database never saw are recovered into the books, and a ledger that
  disagrees with the account balances refuses to start

Live is armed explicitly. Without ``KAUPO_LIVE_TRADING_ENABLED`` and a
credential pair, this module raises before any exchange object exists, so a
misconfigured host places no order at all. Credentials are read from
settings and never logged.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.config import Settings, get_settings
from kaupo.core.engine import Engine, EngineConfig, RunResult
from kaupo.core.funding import DbFundingProvider, EmptyFundingProvider, FundingProvider
from kaupo.core.live_reconcile import ReconciliationRefused, reconcile_live
from kaupo.core.orderflow import DbOrderFlowProvider
from kaupo.core.positioning import DbFuturesMetricsProvider, DbOpenInterestProvider
from kaupo.core.recorder import DbRecorder, RunInfo, RunRecorder
from kaupo.core.resume import ResumeState, prepare_resume
from kaupo.core.runner import DbControlProbe, _chain_persist, _funding_refresh_loop
from kaupo.data.binance import BinanceClient
from kaupo.data.candles import get_latest_candles
from kaupo.data.ingest import LiveCandlePoller, backfill
from kaupo.data.kraken import KrakenClient
from kaupo.db.session import sm_scope
from kaupo.domain import Candle, Fill, Order, Pair, RunMode, RunStatus, Timeframe
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger, LedgerEntry
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy, StrategyBase
from kaupo.venues.kraken_client import (
    AsyncBridge,
    BridgedClient,
    KrakenTradingClient,
    TradingClient,
)
from kaupo.venues.kraken_live import KrakenVenue
from kaupo.venues.paper import LIVE_MIRROR_MARKETABLE_LIMIT

log = logging.getLogger(__name__)


class LiveTradingUnavailable(Exception):
    """Live trading is disarmed or misconfigured; no exchange call was made."""


@dataclass(frozen=True)
class LiveRequest:
    """One live run. Single pair only: portfolio live runs are not supported."""

    strategy: LoadedStrategy
    params: dict[str, Any]
    pair: Pair
    timeframe: Timeframe
    starting_cash: float = 10_000.0
    taker_fee_bps: float = 26.0
    maker_fee_bps: float = 16.0
    slippage_bps: float = 5.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    lookback: int = 300
    # candles of history preloaded from the store; defaults to lookback so
    # live, shadow, and backtest see identical context (parity)
    warmup: int | None = None
    poll_interval_seconds: float = 20.0
    # supervisor-managed runs carry their desired-state row id
    assignment_id: str | None = None
    # seconds between funding-rate refreshes (Binance perp of the base asset)
    funding_refresh_seconds: float = 1800.0


class _ReconciledRecorder:
    """A recorder that writes the reconciliation's recovered fills at run start.

    ``fills.order_id`` and ``fills.run_id`` are both foreign keys, so a
    recovered fill can only be written once the run row exists. This wrapper
    hangs that write off ``start()``, which is the first thing the engine
    does, so the recovered trades are in the database before the first candle
    is processed.
    """

    def __init__(self, inner: RunRecorder, recovered: list[tuple[Order, Fill]]) -> None:
        self._inner = inner
        self._recovered = recovered
        self.run_id = inner.run_id

    async def start(self, info: RunInfo) -> None:
        await self._inner.start(info)
        for order, fill in self._recovered:
            await self._inner.record_order(order)
            await self._inner.record_fill(fill)
        if self._recovered:
            await self._inner.flush()

    async def record_order(self, order: Order) -> None:
        await self._inner.record_order(order)

    async def record_fill(self, fill: Fill) -> None:
        await self._inner.record_fill(fill)

    async def record_ledger(self, entries: list[LedgerEntry]) -> None:
        await self._inner.record_ledger(entries)

    async def record_equity(self, ts: datetime, equity: Decimal, cash: Decimal, unrealized: Decimal) -> None:
        await self._inner.record_equity(ts, equity, cash, unrealized)

    async def finish(self, status: RunStatus, metrics: dict[str, Any] | None) -> None:
        await self._inner.finish(status, metrics)

    async def flush_stale(self) -> None:
        await self._inner.flush_stale()

    async def flush(self) -> None:
        await self._inner.flush()


def check_armed(settings: Settings) -> None:
    """Raise unless live trading is armed and credentialed. Fails fast, fails loud."""
    if not settings.live_trading_enabled:
        raise LiveTradingUnavailable(
            "live trading is disabled; set KAUPO_LIVE_TRADING_ENABLED=true to arm it"
        )
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        raise LiveTradingUnavailable(
            "live trading is armed but the Kraken API credentials are missing "
            "(KAUPO_KRAKEN_API_KEY, KAUPO_KRAKEN_API_SECRET)"
        )
    if settings.live_max_notional <= 0:
        raise LiveTradingUnavailable(
            f"KAUPO_LIVE_MAX_NOTIONAL must be positive, got {settings.live_max_notional}"
        )


async def run_live(
    request: LiveRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
    stop: asyncio.Event | None = None,
    *,
    trading: TradingClient | None = None,
    bridge: AsyncBridge | None = None,
    settings: Settings | None = None,
    funding_client: BinanceClient | None = None,
) -> RunResult:
    """Run one live assignment. ``trading`` and ``bridge`` are injected by tests.

    ``client`` stays the public market-data client: candles come from the
    same poller the shadow runs use, so the two modes see the same stream.
    ``funding_client`` feeds the same advisory funding series a shadow run
    gets; without it the strategy sees an empty series, and any funding
    filter it carries goes quiet.
    """
    settings = settings if settings is not None else get_settings()
    check_armed(settings)
    stop = stop or asyncio.Event()
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, StrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is a portfolio strategy; live runs are single-pair only"
        )

    warmup_candles = await _warm_up(request, sessionmaker, client)
    config = _run_config(request, settings)
    resume = await prepare_resume(
        sessionmaker,
        strategy_id=request.strategy.id,
        strategy_version=request.strategy.version,
        pair=str(request.pair),
        pairs=None,
        timeframe=request.timeframe.value,
        params=request.params,
        quote_asset=request.pair.quote,
        assignment_id=request.assignment_id,
        mode=RunMode.LIVE,
    )
    if resume is not None:
        config["resumed_from"] = resume.predecessor_run_id
        config["chain_started_at"] = resume.chain_started_at
    ledger = (
        Ledger(request.pair.quote, resume.cash, datetime.now(UTC), positions=resume.positions)
        if resume is not None
        else Ledger(request.pair.quote, request.starting_cash, datetime.now(UTC))
    )

    owns_bridge = bridge is None
    bridge = bridge if bridge is not None else AsyncBridge(name=f"kraken-{request.pair}")
    started = False
    try:
        trading = trading if trading is not None else _build_client(bridge, settings)
        # the reconciler runs on this loop, where its database handles live;
        # only its exchange calls hop to the bridge
        reconciled = await reconcile_live(
            BridgedClient(trading, bridge),
            sessionmaker,
            pair=request.pair,
            positions=ledger.open_positions,
            cash=ledger.cash,
            # with no chain there is no history that belongs to this run:
            # the account's own past stays out of the books, and the cash
            # figure is a configured one the balance cannot match either
            history_since=_chain_start(resume),
            check_quote=resume is not None,
            quote_baseline=resume.quote_baseline if resume is not None else None,
            starting_cash=resume.starting_cash if resume is not None else None,
        )
        # the chain's quote reference point for the drift check: inherited from
        # the predecessor, else adopted from the account right now — a fresh
        # start, or a chain whose books predate the relative quote check
        quote_baseline = resume.quote_baseline if resume is not None else None
        if quote_baseline is None:
            quote_baseline = reconciled.balances.get(request.pair.quote, 0.0)
            log.warning(
                "Adopting the current %s balance %.2f as the quote baseline for the %s chain",
                request.pair.quote,
                quote_baseline,
                request.pair,
            )
        config["quote_baseline"] = quote_baseline
        _apply_recovered(ledger, reconciled.missed_fills)
        venue = KrakenVenue(
            request.pair,
            trading,
            bridge,
            max_notional=settings.live_max_notional,
            trades_since_ms=reconciled.trades_cursor_ms,
            seen_trade_ids=reconciled.seen_trade_ids,
        )
        started = True
        return await _run_engine(
            request,
            sessionmaker,
            client,
            stop,
            strategy=strategy,
            ledger=ledger,
            venue=venue,
            config=config,
            warmup_candles=warmup_candles,
            recovered=reconciled.missed,
            funding_client=funding_client,
        )
    except ReconciliationRefused:
        log.error("Live run for %s refused to start: exchange and ledger disagree", request.pair)
        raise
    finally:
        if not started and trading is not None:
            # the engine never ran, so nothing else will close the client
            with suppress(Exception):
                await bridge.run(trading.close())
        if owns_bridge:
            bridge.close()


def _chain_start(resume: ResumeState | None) -> datetime | None:
    """When this run's chain began, or None for a fresh run.

    Reconciliation recovers trades only from this moment on, so a live run
    starting on an account with its own trading past adopts none of it.
    """
    if resume is None:
        return None
    try:
        return datetime.fromisoformat(resume.chain_started_at)
    except ValueError:
        # an unparsable chain root is not a reason to adopt history: fall
        # back to the newest recorded fill, which the reconciler floors to
        log.warning(
            "Chain start %r is not a timestamp; recovering trades from the last recorded fill only",
            resume.chain_started_at,
        )
        return datetime.min.replace(tzinfo=UTC)


def _build_client(bridge: AsyncBridge, settings: Settings) -> TradingClient:
    """Create the authenticated client on the bridge loop, where it will be used."""

    async def _make() -> TradingClient:
        return KrakenTradingClient(settings.kraken_api_key, settings.kraken_api_secret)

    return bridge.run_sync(_make())


async def _warm_up(
    request: LiveRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
) -> list[Candle]:
    """Freshen the candle store and load the run's history, exactly like shadow."""
    warmup = request.warmup if request.warmup is not None else request.lookback
    freshen_since = datetime.now(UTC) - timedelta(seconds=request.timeframe.seconds * (warmup + 5))
    try:
        await backfill(client, sessionmaker, request.pair, request.timeframe, freshen_since)
    except Exception:
        log.warning("Store freshening failed; continuing with existing data", exc_info=True)
    async with sm_scope(sessionmaker) as session:
        candles = await get_latest_candles(session, request.pair, request.timeframe, warmup)
    if len(candles) < warmup // 2:
        log.warning(
            "Only %d of %d warm-up candles for %s %s — run `kaupo ingest candles` for full context",
            len(candles),
            warmup,
            request.pair,
            request.timeframe.value,
        )
    return candles


def _run_config(request: LiveRequest, settings: Settings) -> dict[str, Any]:
    config: dict[str, Any] = {
        "pair": str(request.pair),
        "timeframe": request.timeframe.value,
        "params": request.params,
        "starting_cash": request.starting_cash,
        "fees": {
            "taker_bps": request.taker_fee_bps,
            "maker_bps": request.maker_fee_bps,
            "slippage_bps": request.slippage_bps,
            # not a setting here but a fact: KrakenVenue posts every limit
            # post-only, so a limit marketable at posting is rejected and
            # never fills. The paper venue's "skip" mode imitates this one.
            "marketable_limit": LIVE_MIRROR_MARKETABLE_LIMIT,
        },
        "risk": asdict(request.risk),
        "lookback": request.lookback,
        "warmup": request.warmup if request.warmup is not None else request.lookback,
        # the cap in force for this run, so the audit trail explains a
        # clamped order size long after the host env changed
        "live_max_notional": settings.live_max_notional,
    }
    if request.assignment_id is not None:
        config["assignment_id"] = request.assignment_id
    return config


def _apply_recovered(ledger: Ledger, fills: list[Fill]) -> None:
    """Fold reconciliation's recovered fills into the ledger the run starts with."""
    for fill in fills:
        try:
            ledger.apply_fill(fill)
        except (InsufficientFunds, InsufficientPosition) as exc:
            raise ReconciliationRefused(
                f"A recovered Kraken fill ({fill.side.value} {fill.size} {fill.pair}) does not fit "
                f"the replayed ledger: {exc}. The books need a human before this run can trade."
            ) from exc


async def _run_engine(
    request: LiveRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
    stop: asyncio.Event,
    *,
    strategy: StrategyBase,
    ledger: Ledger,
    venue: KrakenVenue,
    config: dict[str, Any],
    warmup_candles: list[Candle],
    recovered: list[tuple[Order, Fill]],
    funding_client: BinanceClient | None,
) -> RunResult:
    recorder = _ReconciledRecorder(DbRecorder(sessionmaker), recovered)
    # funding stays advisory, exactly as in a shadow run: without a client
    # the series is empty and the strategy must tolerate no data
    funding: FundingProvider = EmptyFundingProvider()
    if funding_client is not None:
        funding = DbFundingProvider(sessionmaker)
    engine = Engine(
        strategy=strategy,
        venue=venue,
        risk=RiskManager(
            replace(
                request.risk,
                taker_fee_bps=request.taker_fee_bps,
                slippage_bps=request.slippage_bps,
            )
        ),
        ledger=ledger,
        recorder=recorder,
        config=EngineConfig(
            pair=request.pair,
            timeframe=request.timeframe,
            lookback=request.lookback,
            liquidate_end=False,  # positions stay open until strategy/risk exits them
        ),
        run_info=RunInfo(
            mode=RunMode.LIVE,
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            strategy_source_hash=request.strategy.source_hash,
            config=config,
        ),
        control_probe=DbControlProbe(sessionmaker, recorder.run_id),
        funding=funding,
        orderflow=DbOrderFlowProvider(sessionmaker),
        open_interest=DbOpenInterestProvider(sessionmaker),
        futures_metrics=DbFuturesMetricsProvider(sessionmaker),
    )
    poller = LiveCandlePoller(
        client,
        request.pair,
        request.timeframe,
        poll_interval_seconds=request.poll_interval_seconds,
        baseline=warmup_candles[-1].ts if warmup_candles else None,
    )
    log.info(
        "Starting LIVE run %s: %s on %s %s (%d warm-up candles)",
        recorder.run_id,
        request.strategy.id,
        request.pair,
        request.timeframe.value,
        len(warmup_candles),
    )
    stream = _chain_persist(warmup_candles, poller, sessionmaker, stop)
    refresh_task: asyncio.Task[None] | None = None
    if funding_client is not None:
        refresh_task = asyncio.create_task(
            _funding_refresh_loop(
                funding_client,
                sessionmaker,
                [request.pair.base],
                request.funding_refresh_seconds,
                stop,
            )
        )
    try:
        result = await engine.run(stream, stop=stop, warmup=len(warmup_candles))
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        venue.close()
    if result.halt_reason:
        from kaupo.core.notify import record_halt

        await record_halt(sessionmaker, recorder.run_id, request.strategy.id, result.halt_reason)
    return result
