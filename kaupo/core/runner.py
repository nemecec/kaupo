"""Shadow trading: the live loop with virtual money.

Same engine, same paper venue as the backtester — only the candle source
differs: historical warm-up from Postgres, then newly closed candles from
the exchange poller (which are also persisted to keep the store fresh).
Portfolio strategies run the same way: one poller per pair, the streams
joined into universe steps identical to the backtest's timestamp join.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import Engine, EngineConfig, RunResult
from kaupo.core.funding import DbFundingProvider, EmptyFundingProvider, FundingProvider
from kaupo.core.portfolio_engine import PortfolioEngine, PortfolioEngineConfig, joined_steps
from kaupo.core.recorder import CompositeRecorder, DbRecorder, InMemoryRecorder, RunInfo
from kaupo.core.resume import prepare_resume
from kaupo.data.binance import BinanceClient
from kaupo.data.candles import get_latest_candles, upsert_candles
from kaupo.data.funding import upsert_funding_rates
from kaupo.data.ingest import LiveCandlePoller, backfill
from kaupo.data.kraken import KrakenClient
from kaupo.db.models import EventRow
from kaupo.db.session import sm_scope
from kaupo.domain import Candle, Pair, RunMode, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy, PortfolioStrategyBase, StrategyBase
from kaupo.venues.paper import PaperVenue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowRequest:
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
    # candles of history preloaded from DB; defaults to lookback so shadow
    # and backtest (prefill = lookback) see identical context (parity)
    warmup: int | None = None
    poll_interval_seconds: float = 20.0
    # supervisor-managed runs carry their desired-state row id
    assignment_id: str | None = None
    # seconds between funding-rate refreshes (Binance perp of the base asset)
    funding_refresh_seconds: float = 1800.0


# funding refresh covers the recent past; older history comes from
# `kaupo ingest funding`
FUNDING_REFRESH_WINDOW = timedelta(days=7)


async def _funding_refresh_loop(
    funding_client: BinanceClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    base_assets: list[str],
    interval_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Keep recent funding for ``base_assets`` fresh until ``stop`` is set.

    Funding is advisory: a failed refresh is logged and retried on the next
    interval, never fatal to the run. One base failing does not block the
    others.
    """
    while not stop.is_set():
        for base in base_assets:
            try:
                since = datetime.now(UTC) - FUNDING_REFRESH_WINDOW
                rates = await funding_client.fetch_funding_rates(base, since=since)
                async with sm_scope(sessionmaker) as session:
                    await upsert_funding_rates(session, rates)
            except Exception:
                log.warning(
                    "Funding refresh failed for %s; retrying in %ss", base, interval_seconds, exc_info=True
                )
        await asyncio.sleep(interval_seconds)


async def _chain_persist(
    warmup_candles: list[Candle],
    poller: LiveCandlePoller,
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> AsyncIterator[Candle]:
    last_ts: datetime | None = None
    for candle in warmup_candles:
        last_ts = candle.ts
        yield candle
    async for candle in poller.stream(stop):
        if last_ts is not None and candle.ts <= last_ts:
            continue  # already covered by warm-up or duplicate
        last_ts = candle.ts
        # deliberate: a persist failure fails the run (restart policy retries)
        # rather than trading on a store that can't record the audit trail
        async with sm_scope(sessionmaker) as session:
            await upsert_candles(session, [candle])
        yield candle


class DbControlProbe:
    """Latest control command (kill/pause/resume/switch) from the events table.

    Commands may target this run specifically or all runs (run_id null).
    "resume" clears a pause; state sticks until a newer command arrives.
    Commands issued before the run started are ignored (a stale global kill
    must not assassinate a fresh run), and a kill or switch, once seen, is
    terminal.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        run_id: str,
        not_before: datetime | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._run_id = run_id
        self._not_before = not_before or datetime.now(UTC)
        self._command: str | None = None

    async def __call__(self) -> str | None:
        if self._command in ("kill", "switch"):
            return self._command  # terminal
        # latest command addressed to this run or to all runs (run_id null),
        # issued after this run started
        async with sm_scope(self._sessionmaker) as session:
            run_id_col = EventRow.data["run_id"].as_string()
            rows = await session.execute(
                select(EventRow)
                .where(
                    EventRow.source == "control",
                    EventRow.ts >= self._not_before,
                    (run_id_col.is_(None)) | (run_id_col == self._run_id),
                )
                .order_by(EventRow.ts.desc())
                .limit(1)
            )
            row = rows.scalars().first()
            if row is not None:
                command = (row.data or {}).get("command")
                if command in ("kill", "pause", "resume", "switch"):
                    self._command = None if command == "resume" else command
        return self._command


async def run_shadow(
    request: ShadowRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
    stop: asyncio.Event | None = None,
    funding_client: BinanceClient | None = None,
) -> RunResult:
    stop = stop or asyncio.Event()
    warmup = request.warmup if request.warmup is not None else request.lookback

    # freshen the store so warm-up reaches the latest closed candle
    freshen_since = datetime.now(UTC) - timedelta(seconds=request.timeframe.seconds * (warmup + 5))
    try:
        await backfill(client, sessionmaker, request.pair, request.timeframe, freshen_since)
    except Exception:
        log.warning("Store freshening failed; continuing with existing data", exc_info=True)

    async with sm_scope(sessionmaker) as session:
        warmup_candles = await get_latest_candles(session, request.pair, request.timeframe, warmup)
    if warmup_candles:
        tail_age = datetime.now(UTC) - warmup_candles[-1].ts
        if tail_age > timedelta(seconds=2 * request.timeframe.seconds):
            log.warning(
                "Warm-up tail is %s old — the store has a data hole; the poller will refill it",
                tail_age,
            )
    if len(warmup_candles) < warmup // 2:
        log.warning(
            "Only %d of %d warm-up candles for %s %s — run `kaupo ingest candles` for full context",
            len(warmup_candles),
            warmup,
            request.pair,
            request.timeframe.value,
        )

    recorder = DbRecorder(sessionmaker)
    config: dict[str, Any] = {
        "pair": str(request.pair),
        "timeframe": request.timeframe.value,
        "params": request.params,
        "starting_cash": request.starting_cash,
        "fees": {
            "taker_bps": request.taker_fee_bps,
            "maker_bps": request.maker_fee_bps,
            "slippage_bps": request.slippage_bps,
        },
        "risk": asdict(request.risk),
        "lookback": request.lookback,
        "warmup": warmup,
    }
    if request.assignment_id is not None:
        # lets the supervisor and the API find the run's desired-state row
        config["assignment_id"] = request.assignment_id
    # a superseded predecessor with an unchanged config passes on its final
    # ledger state (and the run row its lineage); anything else starts fresh
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
    )
    if resume is not None:
        config["resumed_from"] = resume.predecessor_run_id
        config["chain_started_at"] = resume.chain_started_at
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, StrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is a portfolio strategy; shadow runs are single-pair only"
        )
    # without a funding client the run serves an empty series (funding stays
    # advisory; the strategy must tolerate no data)
    funding: FundingProvider = EmptyFundingProvider()
    if funding_client is not None:
        funding = DbFundingProvider(sessionmaker)
    ledger = (
        Ledger(request.pair.quote, resume.cash, datetime.now(UTC), positions=resume.positions)
        if resume is not None
        else Ledger(request.pair.quote, request.starting_cash, datetime.now(UTC))
    )
    engine = Engine(
        strategy=strategy,
        venue=PaperVenue(request.taker_fee_bps, request.maker_fee_bps, request.slippage_bps),
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
            mode=RunMode.SHADOW,
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            strategy_source_hash=request.strategy.source_hash,
            config=config,
        ),
        control_probe=DbControlProbe(sessionmaker, recorder.run_id),
        funding=funding,
    )

    poller = LiveCandlePoller(
        client,
        request.pair,
        request.timeframe,
        poll_interval_seconds=request.poll_interval_seconds,
        baseline=warmup_candles[-1].ts if warmup_candles else None,
    )
    log.info(
        "Starting shadow run %s: %s on %s %s (%d warm-up candles)",
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
    if result.halt_reason:
        from kaupo.core.notify import record_halt

        await record_halt(sessionmaker, recorder.run_id, request.strategy.id, result.halt_reason)
    return result


class _CandleStreamer(Protocol):
    """Anything that streams newly closed candles (LiveCandlePoller in production)."""

    def stream(self, stop: asyncio.Event | None = None) -> AsyncIterator[Candle]: ...


class UniverseCandleJoiner:
    """Join per-pair live candle streams into universe steps.

    Kraken candles of one timeframe close at the same wall-clock instant
    across pairs, so newly closed candles are buffered by timestamp: a step
    is emitted when every pair has delivered its candle for the timestamp,
    or when the grace window passes — a pair that misses a tick simply skips
    it (the engine stale-carries its last known close). Emitted steps have
    strictly increasing timestamps and hold their pairs in sorted order,
    exactly like the backtest's ``joined_steps``: a candle that arrives
    after its step was emitted is dropped (logged), never emitted twice.
    """

    def __init__(self, pollers: Mapping[Pair, _CandleStreamer], grace_seconds: float = 45.0) -> None:
        if len(pollers) < 2:
            raise ValueError("A universe join needs at least 2 pairs")
        self._pollers = dict(pollers)
        self._grace = grace_seconds

    async def stream(self, stop: asyncio.Event) -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
        queue: asyncio.Queue[tuple[Pair, Candle]] = asyncio.Queue()

        async def produce(pair: Pair, poller: _CandleStreamer) -> None:
            async for candle in poller.stream(stop):
                await queue.put((pair, candle))

        tasks = [
            asyncio.create_task(produce(pair, poller), name=f"joiner-{pair}")
            for pair, poller in self._pollers.items()
        ]
        loop = asyncio.get_running_loop()
        pending: dict[datetime, dict[Pair, Candle]] = {}
        deadlines: dict[datetime, float] = {}
        last_emitted: datetime | None = None

        def buffer(pair: Pair, candle: Candle) -> None:
            ts = candle.ts
            if last_emitted is not None and ts <= last_emitted:
                log.warning("Dropping late %s candle at %s: its step is already emitted", pair, ts)
                return
            step = pending.setdefault(ts, {})
            if pair in step:
                log.warning("Dropping duplicate %s candle at %s", pair, ts)
                return
            step[pair] = candle
            deadlines.setdefault(ts, loop.time() + self._grace)

        try:
            while not stop.is_set():
                # wait for the next candle, the nearest grace deadline, or the
                # periodic stop re-check, whichever comes first
                wait = 1.0
                if deadlines:
                    wait = max(0.0, min(wait, min(deadlines.values()) - loop.time()))
                try:
                    pair, candle = await asyncio.wait_for(queue.get(), timeout=wait)
                    buffer(pair, candle)
                except TimeoutError:
                    pass
                # drain whatever else already arrived before emitting partials
                while True:
                    try:
                        pair, candle = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    buffer(pair, candle)
                # emit in timestamp order, like the backtest's joined_steps:
                # a complete step waits for every earlier pending step,
                # which goes out complete or partial at its grace deadline
                for ts in sorted(pending):
                    complete = len(pending[ts]) == len(self._pollers)
                    due = deadlines[ts] <= loop.time()
                    if not (complete or due):
                        break
                    step = pending.pop(ts)
                    del deadlines[ts]
                    last_emitted = ts
                    yield ts, {pair: step[pair] for pair in sorted(step, key=str)}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _chain_persist_universe(
    warmup_by_pair: Mapping[Pair, Sequence[Candle]],
    joiner: UniverseCandleJoiner,
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> AsyncIterator[tuple[datetime, dict[Pair, Candle]]]:
    """Warm-up steps, then live joined steps; polled candles are upserted per pair."""
    last_ts = {pair: candles[-1].ts for pair, candles in warmup_by_pair.items() if candles}
    for ts, step in joined_steps(warmup_by_pair):
        yield ts, dict(step)
    async for ts, step in joiner.stream(stop):
        fresh = {
            pair: candle for pair, candle in step.items() if pair not in last_ts or candle.ts > last_ts[pair]
        }
        if not fresh:
            continue  # already covered by warm-up or duplicate
        last_ts.update({pair: candle.ts for pair, candle in fresh.items()})
        # deliberate: a persist failure fails the run (restart policy retries)
        # rather than trading on a store that can't record the audit trail
        async with sm_scope(sessionmaker) as session:
            await upsert_candles(session, list(fresh.values()))
        yield ts, fresh


@dataclass(frozen=True)
class PortfolioShadowRequest:
    strategy: LoadedStrategy
    params: dict[str, Any]
    pairs: list[Pair]
    timeframe: Timeframe
    starting_cash: float = 10_000.0
    taker_fee_bps: float = 26.0
    maker_fee_bps: float = 16.0
    slippage_bps: float = 5.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    lookback: int = 300
    # candles of history preloaded from DB per pair; defaults to lookback so
    # shadow and backtest (prefill = lookback) see identical context (parity)
    warmup: int | None = None
    poll_interval_seconds: float = 20.0
    # supervisor-managed runs carry their desired-state row id
    assignment_id: str | None = None
    # seconds between funding-rate refreshes (Binance perp per base asset)
    funding_refresh_seconds: float = 1800.0
    # how long the joiner waits for a pair's candle before emitting a
    # partial universe step (the pair skips that tick)
    join_grace_seconds: float = 45.0

    def __post_init__(self) -> None:
        if len(self.pairs) < 2:
            raise ValueError("A portfolio shadow run needs at least 2 pairs; use ShadowRequest for one pair")
        unique = sorted(set(self.pairs), key=str)
        if len(unique) != len(self.pairs):
            raise ValueError(f"Duplicate pairs in universe: {sorted(str(p) for p in self.pairs)}")
        quotes = {pair.quote for pair in unique}
        if len(quotes) != 1:
            raise ValueError(
                f"All pairs of a portfolio run must share one quote currency, got {sorted(quotes)}"
            )
        # canonical order: sorted by pair string, so every downstream
        # iteration (venue stepping, recording) is deterministic
        object.__setattr__(self, "pairs", unique)


async def run_portfolio_shadow(
    request: PortfolioShadowRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
    stop: asyncio.Event | None = None,
    funding_client: BinanceClient | None = None,
) -> RunResult:
    """Shadow-run a portfolio strategy: one poller per pair, joined steps.

    Mirrors ``run_shadow``: per-pair store freshening and warm-up, the
    PortfolioEngine wired exactly like the portfolio backtest (one paper
    venue per pair, one ledger on the shared quote), and polled candles
    persisted to keep the store fresh.
    """
    stop = stop or asyncio.Event()
    warmup = request.warmup if request.warmup is not None else request.lookback

    # freshen the store per pair so warm-up reaches the latest closed candle
    freshen_since = datetime.now(UTC) - timedelta(seconds=request.timeframe.seconds * (warmup + 5))
    for pair in request.pairs:
        try:
            await backfill(client, sessionmaker, pair, request.timeframe, freshen_since)
        except Exception:
            log.warning("Store freshening failed for %s; continuing with existing data", pair, exc_info=True)

    warmup_by_pair: dict[Pair, list[Candle]] = {}
    async with sm_scope(sessionmaker) as session:
        for pair in request.pairs:
            warmup_by_pair[pair] = await get_latest_candles(session, pair, request.timeframe, warmup)
    for pair, candles in warmup_by_pair.items():
        if candles:
            tail_age = datetime.now(UTC) - candles[-1].ts
            if tail_age > timedelta(seconds=2 * request.timeframe.seconds):
                log.warning(
                    "Warm-up tail for %s is %s old — the store has a data hole; the poller will refill it",
                    pair,
                    tail_age,
                )
        if len(candles) < warmup // 2:
            log.warning(
                "Only %d of %d warm-up candles for %s %s — run `kaupo ingest candles` for full context",
                len(candles),
                warmup,
                pair,
                request.timeframe.value,
            )

    recorder = CompositeRecorder([DbRecorder(sessionmaker), InMemoryRecorder()])
    universe = [str(pair) for pair in request.pairs]
    config: dict[str, Any] = {
        # the joined sorted list keeps the config["pair"] shape a plain
        # string, as in single-pair runs
        "pair": ",".join(universe),
        "pairs": universe,
        "timeframe": request.timeframe.value,
        "params": request.params,
        "starting_cash": request.starting_cash,
        "fees": {
            "taker_bps": request.taker_fee_bps,
            "maker_bps": request.maker_fee_bps,
            "slippage_bps": request.slippage_bps,
        },
        "risk": asdict(request.risk),
        "lookback": request.lookback,
        "warmup": warmup,
    }
    if request.assignment_id is not None:
        # lets the supervisor and the API find the run's desired-state row
        config["assignment_id"] = request.assignment_id
    # a superseded predecessor with an unchanged config passes on its final
    # ledger state (and the run row its lineage); anything else starts fresh
    resume = await prepare_resume(
        sessionmaker,
        strategy_id=request.strategy.id,
        strategy_version=request.strategy.version,
        pair=config["pair"],
        pairs=universe,
        timeframe=request.timeframe.value,
        params=request.params,
        quote_asset=request.pairs[0].quote,
        assignment_id=request.assignment_id,
    )
    if resume is not None:
        config["resumed_from"] = resume.predecessor_run_id
        config["chain_started_at"] = resume.chain_started_at
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, PortfolioStrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is not a portfolio strategy; run it with run_shadow (--pair)"
        )
    # without a funding client the run serves an empty series (funding stays
    # advisory; the strategy must tolerate no data)
    funding: FundingProvider = EmptyFundingProvider()
    if funding_client is not None:
        funding = DbFundingProvider(sessionmaker)
    ledger = (
        Ledger(request.pairs[0].quote, resume.cash, datetime.now(UTC), positions=resume.positions)
        if resume is not None
        else Ledger(request.pairs[0].quote, request.starting_cash, datetime.now(UTC))
    )
    engine = PortfolioEngine(
        strategy=strategy,
        venues={
            pair: PaperVenue(request.taker_fee_bps, request.maker_fee_bps, request.slippage_bps)
            for pair in request.pairs
        },
        risk=RiskManager(
            replace(
                request.risk,
                taker_fee_bps=request.taker_fee_bps,
                slippage_bps=request.slippage_bps,
            )
        ),
        ledger=ledger,
        recorder=recorder,
        config=PortfolioEngineConfig(
            pairs=tuple(request.pairs),
            timeframe=request.timeframe,
            lookback=request.lookback,
            liquidate_end=False,  # positions stay open until strategy/risk exits them
        ),
        run_info=RunInfo(
            mode=RunMode.SHADOW,
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            strategy_source_hash=request.strategy.source_hash,
            config=config,
        ),
        control_probe=DbControlProbe(sessionmaker, recorder.run_id),
        funding=funding,
    )

    pollers = {
        pair: LiveCandlePoller(
            client,
            pair,
            request.timeframe,
            poll_interval_seconds=request.poll_interval_seconds,
            baseline=warmup_by_pair[pair][-1].ts if warmup_by_pair[pair] else None,
        )
        for pair in request.pairs
    }
    warmup_steps = sum(1 for _ in joined_steps(warmup_by_pair))
    log.info(
        "Starting portfolio shadow run %s: %s on %s %s (%d warm-up steps)",
        recorder.run_id,
        request.strategy.id,
        ",".join(universe),
        request.timeframe.value,
        warmup_steps,
    )
    joiner = UniverseCandleJoiner(pollers, grace_seconds=request.join_grace_seconds)
    stream = _chain_persist_universe(warmup_by_pair, joiner, sessionmaker, stop)
    refresh_task: asyncio.Task[None] | None = None
    if funding_client is not None:
        refresh_task = asyncio.create_task(
            _funding_refresh_loop(
                funding_client,
                sessionmaker,
                sorted({pair.base for pair in request.pairs}),
                request.funding_refresh_seconds,
                stop,
            )
        )
    try:
        result = await engine.run(stream, stop=stop, warmup=warmup_steps)
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
    if result.halt_reason:
        from kaupo.core.notify import record_halt

        await record_halt(sessionmaker, recorder.run_id, request.strategy.id, result.halt_reason)
    return result
