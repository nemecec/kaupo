"""Shadow trading: the live loop with virtual money.

Same engine, same paper venue as the backtester — only the candle source
differs: historical warm-up from Postgres, then newly closed candles from
the exchange poller (which are also persisted to keep the store fresh).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import Engine, EngineConfig, RunResult
from kaupo.core.recorder import DbRecorder, RunInfo
from kaupo.data.candles import get_candles, upsert_candles
from kaupo.data.ingest import LiveCandlePoller, backfill
from kaupo.data.kraken import KrakenClient
from kaupo.db.session import session_scope
from kaupo.domain import Candle, Pair, RunMode, Timeframe
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import RiskConfig, RiskManager
from kaupo.sdk.protocol import LoadedStrategy
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
    warmup: int = 100  # candles of history preloaded from DB
    poll_interval_seconds: float = 20.0


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
        async with session_scope() as session:
            await upsert_candles(session, [candle])
        yield candle


async def run_shadow(
    request: ShadowRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    client: KrakenClient,
    stop: asyncio.Event | None = None,
) -> RunResult:
    stop = stop or asyncio.Event()

    # freshen the store so warm-up reaches the latest closed candle
    freshen_since = datetime.now(UTC) - timedelta(seconds=request.timeframe.seconds * (request.warmup + 5))
    try:
        await backfill(client, sessionmaker, request.pair, request.timeframe, freshen_since)
    except Exception:
        log.warning("Store freshening failed; continuing with existing data", exc_info=True)

    async with session_scope() as session:
        warmup_candles = await get_candles(
            session, request.pair, request.timeframe, freshen_since, datetime.now(UTC)
        )
    if len(warmup_candles) < request.warmup // 2:
        log.warning(
            "Only %d warm-up candles for %s %s — run `kaupo ingest` for full context",
            len(warmup_candles),
            request.pair,
            request.timeframe.value,
        )

    recorder = DbRecorder(sessionmaker)
    engine = Engine(
        strategy=request.strategy.create(request.params),
        venue=PaperVenue(request.taker_fee_bps, request.maker_fee_bps, request.slippage_bps),
        risk=RiskManager(request.risk),
        ledger=Ledger(request.pair.quote, request.starting_cash, datetime.now(UTC)),
        recorder=recorder,
        config=EngineConfig(
            pair=request.pair,
            timeframe=request.timeframe,
            starting_cash=request.starting_cash,
            lookback=request.lookback,
            liquidate_end=False,  # positions stay open until strategy/risk exits them
        ),
        run_info=RunInfo(
            mode=RunMode.SHADOW,
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            strategy_source_hash=request.strategy.source_hash,
            config={
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
                "warmup": request.warmup,
            },
        ),
    )

    poller = LiveCandlePoller(
        client, request.pair, request.timeframe, poll_interval_seconds=request.poll_interval_seconds
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
    return await engine.run(stream, stop=stop, warmup=len(warmup_candles))
