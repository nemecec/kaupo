"""Shadow trading: the live loop with virtual money.

Same engine, same paper venue as the backtester — only the candle source
differs: historical warm-up from Postgres, then newly closed candles from
the exchange poller (which are also persisted to keep the store fresh).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import Engine, EngineConfig, RunResult
from kaupo.core.recorder import DbRecorder, RunInfo
from kaupo.data.candles import get_latest_candles, upsert_candles
from kaupo.data.ingest import LiveCandlePoller, backfill
from kaupo.data.kraken import KrakenClient
from kaupo.db.models import EventRow
from kaupo.db.session import sm_scope
from kaupo.domain import Candle, Pair, RunMode, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy, StrategyBase
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
            "Only %d of %d warm-up candles for %s %s — run `kaupo ingest` for full context",
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
    strategy = request.strategy.create(request.params)
    if not isinstance(strategy, StrategyBase):
        raise ValueError(
            f"Strategy {request.strategy.id!r} is a portfolio strategy; shadow runs are single-pair only"
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
        ledger=Ledger(request.pair.quote, request.starting_cash, datetime.now(UTC)),
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
    result = await engine.run(stream, stop=stop, warmup=len(warmup_candles))
    if result.halt_reason:
        from kaupo.core.notify import record_halt

        await record_halt(sessionmaker, recorder.run_id, request.strategy.id, result.halt_reason)
    return result
