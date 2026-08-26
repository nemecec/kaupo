"""The portfolio engine: one loop over a multi-pair universe.

Sibling of the proven single-pair :class:`Engine` with the same components
(ledger, paper venue, risk manager, recorders, virtual clock) and the same
per-step order of operations:

1. each pair's venue executes its pending orders against the pair's candle
2. fills are applied to the ledger and recorded
3. one equity snapshot per timestamp, at the last known close per pair
   (stale-price carry: a pair without a candle this step keeps its last close)
4. the risk manager does its time-based checks (daily loss etc.) on the
   portfolio state
5. the strategy sees the step's candles and returns intents
6. the risk manager filters intents; approved orders go to the pair's venue

Determinism: steps iterate the sorted union of candle timestamps; within a
step pairs are processed in sorted pair-string order (venue stepping, fill
application, recording). One venue instance per pair keeps the single-pair
execution semantics exact: a pair's orders only ever fill on that pair's
candles. Backtests feed the steps from a timestamp join of stored candles;
shadow runs feed the same joined steps from live pollers, so strategy step
semantics are identical across modes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kaupo.core.engine import RunResult, VirtualClock
from kaupo.core.funding import EmptyFundingProvider, FundingProvider
from kaupo.core.recorder import RunInfo, RunRecorder
from kaupo.domain import (
    Candle,
    Fill,
    FundingRate,
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
from kaupo.sdk.protocol import PortfolioStrategyBase
from kaupo.venues.venue import Venue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioEngineConfig:
    pairs: tuple[Pair, ...]  # the universe: sorted, unique, one shared quote
    timeframe: Timeframe
    lookback: int = 300
    liquidate_end: bool = False


def joined_steps(
    candles_by_pair: Mapping[Pair, Sequence[Candle]],
) -> Iterator[tuple[datetime, dict[Pair, Candle]]]:
    """Timestamp-join per-pair candle lists (each ascending by ts).

    Yields ``(ts, {pair: candle})`` for the sorted union of timestamps; a
    step holds exactly the pairs with a candle at that timestamp, keyed in
    sorted pair-string order. Pairs with a missing candle simply skip the
    step — no fabrication.
    """
    queues = {pair: deque(candles) for pair, candles in candles_by_pair.items()}
    while any(queues.values()):
        ts = min(queue[0].ts for queue in queues.values() if queue)
        step: dict[Pair, Candle] = {}
        for pair in sorted(queues, key=str):
            queue = queues[pair]
            if queue and queue[0].ts == ts:
                step[pair] = queue.popleft()
        yield ts, step


class _PortfolioContext:
    def __init__(self, engine: PortfolioEngine) -> None:
        self._engine = engine

    @property
    def clock(self) -> VirtualClock:
        return self._engine.clock

    @property
    def candles(self) -> Mapping[Pair, Candle]:
        return self._engine._step_candles

    def history(self, pair: Pair, n: int) -> Sequence[Candle]:
        if n <= 0:
            return []
        engine = self._engine
        hist = engine.history.get(pair)
        if hist is None:
            log.warning("Strategy requested history for pair %s outside the universe", pair)
            return []
        maxlen = hist.maxlen
        if maxlen is not None and n > maxlen and pair not in engine._warned_history_cap:
            engine._warned_history_cap.add(pair)
            log.warning(
                "Strategy requested history(%d) beyond lookback=%d for %s; it will always "
                "receive fewer candles (increase lookback)",
                n,
                maxlen,
                pair,
            )
        return list(hist)[-n:] if n < len(hist) else list(hist)

    def funding(self, pair: Pair, n: int) -> Sequence[FundingRate]:
        if n <= 0:
            return []
        engine = self._engine
        if pair not in engine.history:
            log.warning("Strategy requested funding for pair %s outside the universe", pair)
            return []
        return engine._funding.latest(pair.base, n, engine.clock.now())

    def positions(self) -> Mapping[Pair, Position]:
        engine = self._engine
        return {pair: pos for pair in engine.config.pairs if (pos := engine.ledger.position(pair)).size != 0}

    def cash(self) -> float:
        return float(self._engine.ledger.cash)

    def equity(self) -> float:
        return float(self._engine.ledger.equity(self._engine.last_closes))


class PortfolioEngine:
    def __init__(
        self,
        strategy: PortfolioStrategyBase,
        venues: Mapping[Pair, Venue],
        risk: RiskManager,
        ledger: Ledger,
        recorder: RunRecorder,
        config: PortfolioEngineConfig,
        run_info: RunInfo,
        control_probe: Callable[[], Awaitable[str | None]] | None = None,
        funding: FundingProvider | None = None,
    ) -> None:
        self.strategy = strategy
        self.venues = dict(venues)
        self.risk = risk
        self.ledger = ledger
        self.recorder = recorder
        self.config = config
        self.run_info = run_info
        self.clock = VirtualClock(config.timeframe)
        self.history: dict[Pair, deque[Candle]] = {
            pair: deque(maxlen=config.lookback) for pair in config.pairs
        }
        self._funding = funding if funding is not None else EmptyFundingProvider()
        self.last_closes: dict[Pair, float] = {}  # last known close per pair (stale carry)
        self._last_candle: dict[Pair, Candle] = {}
        self._step_candles: dict[Pair, Candle] = {}
        self._ctx = _PortfolioContext(self)
        self._fills = 0
        self._halt_reason = ""
        self._control_probe = control_probe
        self._killed = False
        self._kill_reason = ""
        self._last_snapshot_ts: datetime | None = None
        self._warned_history_cap: set[Pair] = set()
        missing = set(config.pairs) - set(self.venues)
        if missing:
            raise ValueError(f"No venue for universe pairs: {sorted(str(p) for p in missing)}")

    async def run(
        self,
        steps: AsyncIterable[tuple[datetime, dict[Pair, Candle]]],
        stop: asyncio.Event | None = None,
        warmup: int = 0,
    ) -> RunResult:
        """Run the loop. The first ``warmup`` steps only populate history and
        last-known prices — no orders, no snapshots."""
        await self.recorder.start(self.run_info)
        status = RunStatus.COMPLETED
        last_step: dict[Pair, Candle] | None = None
        seen = 0
        try:
            async for ts, candles in steps:
                if stop is not None and stop.is_set():
                    status = RunStatus.HALTED
                    self._halt_reason = "stopped externally"
                    break
                last_step = candles
                if seen % 100 == 0:
                    await asyncio.sleep(0)  # keep the event loop responsive
                self._track(ts, candles)
                if seen < warmup:
                    self._append_history(candles)
                else:
                    await self._process_step(ts, candles)
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
            final_equity = await self._wind_down(status, last_step)
        return RunResult(
            status=status,
            final_equity=final_equity,
            num_fills=self._fills,
            halt_reason=self._halt_reason,
        )

    def _track(self, ts: datetime, candles: dict[Pair, Candle]) -> None:
        """Advance clock, last-known prices, and per-pair last candles."""
        self.clock.set(candles[sorted(candles, key=str)[0]])  # all share ts
        for pair, candle in candles.items():
            self.last_closes[pair] = candle.close
            self._last_candle[pair] = candle

    def _append_history(self, candles: dict[Pair, Candle]) -> None:
        for pair, candle in candles.items():
            self.history[pair].append(candle)

    async def _process_step(self, ts: datetime, candles: dict[Pair, Candle]) -> None:
        # 1-2. per pair (sorted order): execute pending orders, apply fills
        for pair in sorted(candles, key=str):
            venue = self.venues[pair]
            fills = venue.on_candle(candles[pair])
            for order in venue.drain_new_orders():
                await self.recorder.record_order(order)  # protection exits created by venue
            for order in venue.drain_expired():
                await self.recorder.record_order(order)  # limit orders expired untouched
            for fill in fills:
                try:
                    realized = self.ledger.apply_fill(fill)
                except (InsufficientFunds, InsufficientPosition) as exc:
                    # the risk manager should prevent this; demote to a rejected
                    # order and keep venue/ledger/audit consistent
                    log.error("Ledger rejected fill %s %s: %s", fill.side.value, fill.pair, exc)
                    venue.void_fill(fill)
                    self.risk.rejections.append(f"ledger rejected {fill.side.value}: {exc}")
                    continue
                if fill.side is Side.SELL:
                    self.risk.notify_trade_result(float(realized))
                await self.recorder.record_fill(fill)
                self._fills += 1
            for order in self._orders_touched(venue, fills):
                await self.recorder.record_order(order)  # upsert final state
        await self.recorder.record_ledger(self.ledger.drain_entries())
        await self.recorder.flush_stale()  # rows land at least per step end

        # 3. equity snapshot at the step's close, last known price per pair
        equity = self.ledger.equity(self.last_closes)
        await self.recorder.record_equity(ts, equity, self.ledger.cash, self._unrealized())
        self._last_snapshot_ts = ts

        # 4. risk time-based checks on the portfolio state
        if not self.risk.on_candle(self._risk_state()):
            return

        # external control: kill/switch halt, pause skips strategy actions
        if self._control_probe is not None:
            command = await self._control_probe()
            if command in ("kill", "switch"):
                # a switch is a graceful kill: the supervisor brings the run
                # back up on the new assignment row
                self._killed = True
                self._kill_reason = (
                    "strategy switch requested" if command == "switch" else "killed via control API"
                )
                return
            if command == "pause":
                log.info("Run paused; skipping strategy for %s", ts)
                self._append_history(candles)
                return

        # 5-6. strategy + risk-filtered intents
        for base in sorted({pair.base for pair in self.config.pairs}):
            await self._funding.update(base, self.clock.now())
        self._append_history(candles)
        self._step_candles = candles
        intents = self.strategy.on_candle(self._ctx)
        valid: list[OrderIntent] = []
        for intent in intents:
            if intent.pair not in self.history:
                # explicit rejection: a foreign pair would otherwise fill
                # against the wrong clock
                log.error("Intent for foreign pair %s rejected (not in universe)", intent.pair)
                self.risk.rejections.append(f"foreign pair {intent.pair}: not in universe")
                continue
            valid.append(intent)
        for assessment in self.risk.assess(valid, self._risk_state()):
            if assessment.decision is Decision.REJECTED:
                log.debug("Intent rejected: %s", assessment.reason)
                continue
            order = self._order_from(assessment.intent, assessment.size, ts)
            self.venues[order.pair].submit(order)
            await self.recorder.record_order(order)

    def _orders_touched(self, venue: Venue, fills: list[Fill]) -> list[Order]:
        # venue mutates orders in place; re-record those that filled
        seen: set[str] = set()
        touched: list[Order] = []
        for fill in fills:
            order = venue.get_order(fill.order_id)
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

    def _risk_state(self) -> RiskState:
        return RiskState(
            ts=self.clock.now(),
            cash=float(self.ledger.cash),
            positions={pair: self.ledger.position(pair) for pair in self.config.pairs},
            prices=dict(self.last_closes),
            equity=float(self.ledger.equity(self.last_closes)),
        )

    def _unrealized(self) -> Decimal:
        total = Decimal(0)
        for pair in self.config.pairs:
            pos = self.ledger.position(pair)
            if pos.size and pair in self.last_closes:
                total += Decimal(str(pos.size)) * (
                    Decimal(str(self.last_closes[pair])) - Decimal(str(pos.avg_entry))
                )
        return total

    async def _wind_down(self, status: RunStatus, last_step: dict[Pair, Candle] | None) -> Decimal:
        if status is RunStatus.COMPLETED and self.config.liquidate_end and last_step is not None:
            for pair in sorted(self.config.pairs, key=str):
                pos = self.ledger.position(pair)
                candle = self._last_candle.get(pair)
                if pos.size <= 0 or candle is None:
                    continue
                fill = self.venues[pair].liquidate(pair, pos.size, candle)
                realized = self.ledger.apply_fill(fill)
                self.risk.notify_trade_result(float(realized))
                await self.recorder.record_fill(fill)
                order = self.venues[pair].get_order(fill.order_id)
                if order is not None:
                    await self.recorder.record_order(order)
                self._fills += 1
            await self.recorder.record_ledger(self.ledger.drain_entries())
            latest = max((c.ts for c in self._last_candle.values()), default=None)
            if latest is not None and latest != self._last_snapshot_ts:
                equity = self.ledger.equity(self.last_closes)
                await self.recorder.record_equity(latest, equity, self.ledger.cash, Decimal(0))
                self._last_snapshot_ts = latest

        for pair in sorted(self.venues, key=str):
            for order in self.venues[pair].cancel_all():
                if order.status is OrderStatus.CANCELLED:
                    await self.recorder.record_order(order)

        # best-effort: persist any ledger entries left behind by a failure
        leftover = self.ledger.drain_entries()
        if leftover:
            try:
                await self.recorder.record_ledger(leftover)
            except Exception:
                log.warning("Could not persist leftover ledger entries", exc_info=True)

        final_equity = self.ledger.equity(self.last_closes)
        try:
            await self.recorder.finish(status, metrics=None)  # metrics set by caller
        except Exception:
            log.error("Could not mark run as %s", status.value, exc_info=True)
        return final_equity
