"""The engine: one loop, three modes.

Backtest, shadow, and live all run this exact loop; only the candle source
and the venue differ. Per candle:

1. the venue executes pending orders against the candle (fills)
2. fills are applied to the ledger and recorded
3. equity is snapshotted
4. the risk manager does its time-based checks (daily loss etc.)
5. the strategy sees the closed candle and returns intents
6. the risk manager filters intents; approved orders go to the venue
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from kaupo.core.recorder import RunInfo, RunRecorder
from kaupo.domain import (
    Candle,
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    Pair,
    Position,
    RunStatus,
    Side,
    Timeframe,
)
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger
from kaupo.risk.manager import Decision, RiskManager, RiskState
from kaupo.sdk.protocol import StrategyBase
from kaupo.venues.venue import Venue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineConfig:
    pair: Pair
    timeframe: Timeframe
    lookback: int = 300
    liquidate_end: bool = False


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    final_equity: Decimal
    num_fills: int
    halt_reason: str = ""


class VirtualClock:
    """Candle-driven clock: now() is the current candle's close time."""

    def __init__(self, timeframe: Timeframe) -> None:
        self._timeframe = timeframe
        self._current: datetime | None = None

    def set(self, candle: Candle) -> None:
        self._current = candle.ts

    def now(self) -> datetime:
        assert self._current is not None
        return self._current + timedelta(seconds=self._timeframe.seconds)


class _Context:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def clock(self) -> VirtualClock:
        return self._engine.clock

    @property
    def candle(self) -> Candle:
        return self._engine.history[-1]

    def history(self, n: int) -> Sequence[Candle]:
        engine = self._engine
        maxlen = engine.history.maxlen
        if maxlen is not None and n > maxlen and not engine._warned_history_cap:
            engine._warned_history_cap = True
            log.warning(
                "Strategy requested history(%d) beyond lookback=%d; it will always "
                "receive fewer candles (increase lookback)",
                n,
                maxlen,
            )
        hist = engine.history
        return list(hist)[-n:] if n < len(hist) else list(hist)

    def position(self) -> Position:
        return self._engine.ledger.position(self._engine.config.pair)

    def cash(self) -> float:
        return float(self._engine.ledger.cash)

    def equity(self) -> float:
        return float(self._engine.ledger.equity({self._engine.config.pair: self.candle.close}))


class Engine:
    def __init__(
        self,
        strategy: StrategyBase,
        venue: Venue,
        risk: RiskManager,
        ledger: Ledger,
        recorder: RunRecorder,
        config: EngineConfig,
        run_info: RunInfo,
        control_probe: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.strategy = strategy
        self.venue = venue
        self.risk = risk
        self.ledger = ledger
        self.recorder = recorder
        self.config = config
        self.run_info = run_info
        self.clock = VirtualClock(config.timeframe)
        self.history: deque[Candle] = deque(maxlen=config.lookback)
        self._ctx = _Context(self)
        self._fills = 0
        self._halt_reason = ""
        self._control_probe = control_probe
        self._killed = False
        self._last_snapshot_ts: datetime | None = None
        self._warned_history_cap = False

    async def run(
        self, candles: AsyncIterable[Candle], stop: asyncio.Event | None = None, warmup: int = 0
    ) -> RunResult:
        """Run the loop. The first ``warmup`` candles only populate history —
        no orders, no snapshots — so a live run starts with full context."""
        await self.recorder.start(self.run_info)
        status = RunStatus.COMPLETED
        last_candle: Candle | None = None
        seen = 0
        try:
            async for candle in candles:
                if stop is not None and stop.is_set():
                    status = RunStatus.HALTED
                    self._halt_reason = "stopped externally"
                    break
                last_candle = candle
                if seen % 100 == 0:
                    await asyncio.sleep(0)  # keep the event loop responsive
                if seen < warmup:
                    self.clock.set(candle)
                    self.history.append(candle)
                else:
                    await self._process_candle(candle)
                seen += 1
                if self._killed:
                    status = RunStatus.HALTED
                    self._halt_reason = "killed via control API"
                    log.warning("Run killed via control API")
                    break
                if self.risk.halted:
                    status = RunStatus.HALTED
                    self._halt_reason = self.risk.halt_reason
                    log.warning("Run halted by risk manager: %s", self._halt_reason)
                    break
        except Exception:
            status = RunStatus.FAILED
            log.exception("Run failed")
            raise
        finally:
            final_equity = await self._wind_down(status, last_candle)
        return RunResult(
            status=status,
            final_equity=final_equity,
            num_fills=self._fills,
            halt_reason=self._halt_reason,
        )

    async def _process_candle(self, candle: Candle) -> None:
        self.clock.set(candle)

        # 1-2. execute pending orders, apply fills
        fills = self.venue.on_candle(candle)
        for order in self.venue.drain_new_orders():
            await self.recorder.record_order(order)  # protection exits created by venue
        for fill in fills:
            try:
                realized = self.ledger.apply_fill(fill)
            except (InsufficientFunds, InsufficientPosition) as exc:
                # the risk manager should prevent this; treat as a rejected
                # order rather than killing the run
                log.error("Ledger rejected fill %s %s: %s", fill.side.value, fill.pair, exc)
                self.risk.rejections.append(f"ledger rejected {fill.side.value}: {exc}")
                continue
            if fill.side is Side.SELL:
                self.risk.notify_trade_result(float(realized))
            await self.recorder.record_fill(fill)
            self._fills += 1
        for order in self._orders_touched(fills):
            await self.recorder.record_order(order)  # upsert final state
        await self.recorder.record_ledger(self.ledger.drain_entries())

        # 3. equity snapshot at close
        price = candle.close
        equity = self.ledger.equity({self.config.pair: price})
        unrealized = self._unrealized(price)
        await self.recorder.record_equity(candle.ts, equity, self.ledger.cash, unrealized)
        self._last_snapshot_ts = candle.ts

        # 4. risk time-based checks
        if not self.risk.on_candle(self._risk_state(candle)):
            return

        # external control: kill halts, pause skips strategy actions
        if self._control_probe is not None:
            command = await self._control_probe()
            if command == "kill":
                self._killed = True
                return
            if command == "pause":
                log.info("Run paused; skipping strategy for %s", candle.ts)
                self.history.append(candle)
                return

        # 5-6. strategy + risk-filtered intents
        self.history.append(candle)
        intents = self.strategy.on_candle(self._ctx)
        for assessment in self.risk.assess(intents, self._risk_state(candle)):
            if assessment.decision is Decision.REJECTED:
                log.debug("Intent rejected: %s", assessment.reason)
                continue
            order = self._order_from(assessment.intent, assessment.size, candle.ts)
            self.venue.submit(order)
            await self.recorder.record_order(order)

    def _orders_touched(self, fills: list[Fill]) -> list[Order]:
        # venue mutates orders in place; re-record those that filled
        seen: set[str] = set()
        touched: list[Order] = []
        for fill in fills:
            order = self.venue.get_order(fill.order_id)
            if order is not None and order.id not in seen:
                seen.add(order.id)
                touched.append(order)
        return touched

    def _order_from(self, intent: OrderIntent, size: float, ts: datetime) -> Order:
        return Order(
            pair=intent.pair,
            side=intent.side,
            order_type=intent.order_type,
            size=size,
            limit_price=intent.limit_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            reason=intent.reason,
            created_ts=ts,
        )

    def _risk_state(self, candle: Candle) -> RiskState:
        price = candle.close
        return RiskState(
            ts=self.clock.now(),
            cash=float(self.ledger.cash),
            positions={self.config.pair: self.ledger.position(self.config.pair)},
            prices={self.config.pair: price},
            equity=float(self.ledger.equity({self.config.pair: price})),
        )

    def _unrealized(self, price: float) -> Decimal:
        pos = self.ledger.position(self.config.pair)
        return Decimal(str(pos.size)) * (Decimal(str(price)) - Decimal(str(pos.avg_entry)))

    async def _wind_down(self, status: RunStatus, last_candle: Candle | None) -> Decimal:
        if status is RunStatus.COMPLETED and self.config.liquidate_end and last_candle is not None:
            pos = self.ledger.position(self.config.pair)
            if pos.size > 0:
                fill = self.venue.liquidate(self.config.pair, pos.size, last_candle)
                realized = self.ledger.apply_fill(fill)
                self.risk.notify_trade_result(float(realized))
                await self.recorder.record_fill(fill)
                order = self.venue.get_order(fill.order_id)
                if order is not None:
                    await self.recorder.record_order(order)
                await self.recorder.record_ledger(self.ledger.drain_entries())
                self._fills += 1
                if last_candle.ts != self._last_snapshot_ts:
                    equity = self.ledger.equity({self.config.pair: last_candle.close})
                    await self.recorder.record_equity(last_candle.ts, equity, self.ledger.cash, Decimal(0))
                    self._last_snapshot_ts = last_candle.ts

        for order in self.venue.cancel_all():
            if order.status is OrderStatus.CANCELLED:
                await self.recorder.record_order(order)

        # best-effort: persist any ledger entries left behind by a failure
        leftover = self.ledger.drain_entries()
        if leftover:
            try:
                await self.recorder.record_ledger(leftover)
            except Exception:
                log.warning("Could not persist leftover ledger entries", exc_info=True)

        price = last_candle.close if last_candle is not None else 0.0
        final_equity = self.ledger.equity({self.config.pair: price})
        try:
            await self.recorder.finish(status, metrics=None)  # metrics set by caller
        except Exception:
            log.error("Could not mark run as %s", status.value, exc_info=True)
        return final_equity
