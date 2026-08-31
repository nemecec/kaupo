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

from kaupo.core.funding import EmptyFundingProvider, FundingProvider
from kaupo.core.orderflow import EmptyOrderFlowProvider, OrderFlowProvider
from kaupo.core.positioning import (
    EmptyFuturesMetricsProvider,
    EmptyOpenInterestProvider,
    FuturesMetricsProvider,
    OpenInterestProvider,
)
from kaupo.core.recorder import RunInfo, RunRecorder
from kaupo.domain import (
    BookSnapshot,
    Candle,
    Fill,
    FundingRate,
    FuturesMetricsDaily,
    OpenInterest,
    Order,
    OrderflowDaily,
    OrderIntent,
    OrderStatus,
    OrderType,
    Pair,
    Position,
    RunStatus,
    Side,
    TickFlow,
    Timeframe,
    TradeTick,
)
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger
from kaupo.risk.manager import Decision, RiskManager, RiskState
from kaupo.sdk.protocol import StrategyBase
from kaupo.venues.venue import Venue

log = logging.getLogger(__name__)

# halt reason when the run's own stop event ends it (deploy, shutdown, CLI
# stop): a graceful external stop, distinct from rail halts and control
# kills — the resume logic reads it back from the audit log
STOPPED_EXTERNALLY = "stopped externally"

# Upper bound for one candle body (venue, fills, ledger, snapshot, strategy).
# Bodies normally take milliseconds; a hang here is the 2026-08-31 silent-stall
# shape, so it must fail loudly and let the supervisor restart the run.
CANDLE_BODY_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class EngineConfig:
    pair: Pair
    timeframe: Timeframe
    lookback: int = 300
    liquidate_end: bool = False
    instrument: str = "spot"  # "spot": long-only. "perp": shorts + funding + liquidation rail


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
        if n <= 0:
            return []
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

    def funding(self, n: int) -> Sequence[FundingRate]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._funding.latest(engine.config.pair.base, n, engine.clock.now())

    def ticks(self, n: int) -> Sequence[TradeTick]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._orderflow.ticks(str(engine.config.pair), n, engine.clock.now())

    def book(self, n: int) -> Sequence[BookSnapshot]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._orderflow.book(str(engine.config.pair), n, engine.clock.now())

    def tick_flow(self, n: int) -> Sequence[TickFlow]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._orderflow.tick_flow(
            str(engine.config.pair), n, engine.clock.now(), engine.config.timeframe.seconds
        )

    def tick_flow_daily(self, n: int) -> Sequence[OrderflowDaily]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._orderflow.tick_flow_daily(str(engine.config.pair), n, engine.clock.now())

    def open_interest(self, n: int) -> Sequence[OpenInterest]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._open_interest.latest(engine.config.pair.base, n, engine.clock.now())

    def futures_metrics_daily(self, n: int) -> Sequence[FuturesMetricsDaily]:
        if n <= 0:
            return []
        engine = self._engine
        return engine._futures_metrics.latest(engine.config.pair.base, n, engine.clock.now())

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
        funding: FundingProvider | None = None,
        orderflow: OrderFlowProvider | None = None,
        open_interest: OpenInterestProvider | None = None,
        futures_metrics: FuturesMetricsProvider | None = None,
        candle_timeout_seconds: float = CANDLE_BODY_TIMEOUT_SECONDS,
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
        self._funding = funding if funding is not None else EmptyFundingProvider()
        self._orderflow = orderflow if orderflow is not None else EmptyOrderFlowProvider()
        self._open_interest = open_interest if open_interest is not None else EmptyOpenInterestProvider()
        self._futures_metrics = (
            futures_metrics if futures_metrics is not None else EmptyFuturesMetricsProvider()
        )
        self._fills = 0
        self._halt_reason = ""
        self._control_probe = control_probe
        self._killed = False
        self._kill_reason = ""
        self._last_snapshot_ts: datetime | None = None
        self._last_funding_ts: datetime | None = None
        self._warned_history_cap = False
        self._candle_timeout = candle_timeout_seconds

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
                    self._halt_reason = STOPPED_EXTERNALLY
                    break
                last_candle = candle
                if seen % 100 == 0:
                    await asyncio.sleep(0)  # keep the event loop responsive
                if seen < warmup:
                    self.clock.set(candle)
                    self.history.append(candle)
                else:
                    try:
                        async with asyncio.timeout(self._candle_timeout):
                            await self._process_candle(candle)
                    except TimeoutError:
                        log.error(
                            "Candle body watchdog fired after %.0fs at %s", self._candle_timeout, candle.ts
                        )
                        raise
                seen += 1
                if self._killed:
                    status = RunStatus.HALTED
                    self._halt_reason = self._kill_reason
                    log.warning("Run halted via control: %s", self._kill_reason)
                    break
                if self.risk.halted:
                    status = RunStatus.HALTED
                    self._halt_reason = self.risk.halt_reason
                    log.warning("Run halted by risk manager: %s", self._halt_reason)
                    break
        except BaseException:
            # Exception *and* SystemExit/KeyboardInterrupt: the run failed.
            # (wind_down only liquidates on COMPLETED, so no phantom trades.)
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
        for order in self.venue.drain_expired():
            await self.recorder.record_order(order)  # limit orders expired untouched
        for fill in fills:
            try:
                realized = self.ledger.apply_fill(fill)
            except (InsufficientFunds, InsufficientPosition) as exc:
                # the risk manager should prevent this; demote to a rejected
                # order and keep venue/ledger/audit consistent
                log.error("Ledger rejected fill %s %s: %s", fill.side.value, fill.pair, exc)
                self.venue.void_fill(fill)
                self.risk.rejections.append(f"ledger rejected {fill.side.value}: {exc}")
                continue
            if fill.side is Side.SELL or (self.config.instrument == "perp" and realized != 0):
                # perp: covering BUYs realize PnL too; zero-PnL entries leave
                # the streak unchanged either way
                self.risk.notify_trade_result(float(realized))
            await self.recorder.record_fill(fill)
            self._fills += 1
        for order in self._orders_touched(fills):
            await self.recorder.record_order(order)  # upsert final state
        if self.config.instrument == "perp":
            await self._apply_funding(candle)
        await self.recorder.record_ledger(self.ledger.drain_entries())
        await self.recorder.flush_stale()  # rows land at least per candle end

        # 3. equity snapshot at close
        price = candle.close
        equity = self.ledger.equity({self.config.pair: price})
        unrealized = self._unrealized(price)
        await self.recorder.record_equity(candle.ts, equity, self.ledger.cash, unrealized)
        self._last_snapshot_ts = candle.ts

        # 3b. perp liquidation rail: equity at/below zero force-closes at mark
        if self.config.instrument == "perp" and equity <= 0:
            await self._liquidate(candle)
            self.risk.halted = True
            self.risk.halt_reason = f"liquidated: equity {equity:.2f} depleted at close {candle.close}"
            return

        # 4. risk time-based checks
        if not self.risk.on_candle(self._risk_state(candle)):
            return

        # external control: kill/switch halt, pause skips strategy actions
        if self._control_probe is not None:
            command = await self._control_probe()
            if command in ("kill", "switch"):
                # a switch is a graceful kill: the container restart policy
                # brings the run back up on the new settings
                self._killed = True
                self._kill_reason = (
                    "strategy switch requested" if command == "switch" else "killed via control API"
                )
                return
            if command == "pause":
                log.info("Run paused; skipping strategy for %s", candle.ts)
                self.history.append(candle)
                return

        # 5-6. strategy + risk-filtered intents
        await self._funding.update(self.config.pair.base, self.clock.now())
        await self._orderflow.update(str(self.config.pair), self.clock.now())
        await self._open_interest.update(self.config.pair.base, self.clock.now())
        await self._futures_metrics.update(self.config.pair.base, self.clock.now())
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

    async def _apply_funding(self, candle: Candle) -> None:
        """Charge perpetual funding for events in (candle open, candle close].

        The position after this candle's fills stands in for the position
        during the interval (positions change only at candle boundaries in
        this engine); the candle close is the funding mark. The sign follows
        the venue convention: a positive rate means longs pay shorts.
        """
        since = self._last_funding_ts if self._last_funding_ts is not None else candle.ts
        points = self._funding.latest(self.config.pair.base, 10, self.clock.now())
        new_events = [p for p in points if p.ts > since]
        if not new_events:
            return
        self._last_funding_ts = new_events[-1].ts  # advances even while flat
        pos = self.ledger.position(self.config.pair)
        if pos.size == 0:
            return
        for point in new_events:
            payment = Decimal(str(-pos.size * candle.close * point.rate))
            self.ledger.apply_funding(point.ts, payment)

    async def _liquidate(self, candle: Candle) -> None:
        """Force-close the whole position at the candle close (perp rail)."""
        pos = self.ledger.position(self.config.pair)
        if pos.size == 0:
            return
        side = Side.BUY if pos.size < 0 else Side.SELL
        fee = abs(pos.size) * candle.close * (self.risk.config.taker_fee_bps / 10_000)
        order = Order(
            pair=self.config.pair,
            side=side,
            order_type=OrderType.MARKET,
            size=abs(pos.size),
            reason="liquidation",
        )
        order.status = OrderStatus.FILLED
        order.filled_price = candle.close
        order.filled_ts = self.clock.now()
        order.fee = fee
        fill = Fill(
            order_id=order.id,
            pair=self.config.pair,
            side=side,
            ts=self.clock.now(),
            price=candle.close,
            size=abs(pos.size),
            fee=fee,
        )
        self.ledger.apply_liquidation_fill(fill)
        await self.recorder.record_order(order)
        await self.recorder.record_fill(fill)
        await self.recorder.record_ledger(self.ledger.drain_entries())
        self._fills += 1

    async def _wind_down(self, status: RunStatus, last_candle: Candle | None) -> Decimal:
        if status is RunStatus.COMPLETED and self.config.liquidate_end and last_candle is not None:
            pos = self.ledger.position(self.config.pair)
            if pos.size != 0:  # a perp run can end short: cover it too
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
