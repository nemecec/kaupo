"""Run persistence: what happened during a run, stored for review and reports."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.db.models import (
    EquitySnapshotRow,
    FillRow,
    LedgerEntryRow,
    OrderRow,
    RunRow,
    StrategyRow,
)
from kaupo.domain import Fill, Order, RunId, RunMode, RunStatus, new_id, utc_now
from kaupo.ledger.ledger import LedgerEntry


@dataclass(frozen=True)
class RunInfo:
    mode: RunMode
    strategy_id: str
    strategy_version: str
    strategy_source_hash: str
    config: dict[str, Any]  # pair, timeframe, params, fees, risk config...


class RunRecorder(Protocol):
    run_id: RunId

    async def start(self, info: RunInfo) -> None: ...
    async def record_order(self, order: Order) -> None: ...
    async def record_fill(self, fill: Fill) -> None: ...
    async def record_ledger(self, entries: list[LedgerEntry]) -> None: ...
    async def record_equity(
        self, ts: datetime, equity: Decimal, cash: Decimal, unrealized: Decimal
    ) -> None: ...
    async def finish(self, status: RunStatus, metrics: dict[str, Any] | None) -> None: ...


class DbRecorder:
    """Buffers rows and flushes in batches to Postgres."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], flush_every: int = 500) -> None:
        self._sessionmaker = sessionmaker
        self._flush_every = flush_every
        self.run_id = RunId(new_id())
        self._orders: list[OrderRow] = []
        self._fills: list[FillRow] = []
        self._ledger: list[LedgerEntryRow] = []
        self._equity: list[EquitySnapshotRow] = []

    async def start(self, info: RunInfo) -> None:
        async with self._sessionmaker() as session:
            stmt = (
                pg_insert(StrategyRow)
                .values(
                    id=info.strategy_id,
                    version=info.strategy_version,
                    source_hash=info.strategy_source_hash,
                    params=info.config.get("params", {}),
                )
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)
            session.add(
                RunRow(
                    id=self.run_id,
                    mode=info.mode.value,
                    strategy_id=info.strategy_id,
                    strategy_version=info.strategy_version,
                    started_at=utc_now(),
                    status=RunStatus.RUNNING.value,
                    config=info.config,
                )
            )
            await session.commit()

    async def record_order(self, order: Order) -> None:
        self._orders.append(
            OrderRow(
                id=order.id,
                run_id=self.run_id,
                ts=order.created_ts,
                pair=str(order.pair),
                side=order.side.value,
                type=order.order_type.value,
                size=order.size,
                limit_price=order.limit_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                status=order.status.value,
                filled_price=order.filled_price,
                filled_ts=order.filled_ts,
                fee=order.fee,
                reason=order.reason,
            )
        )
        await self._maybe_flush()

    async def record_fill(self, fill: Fill) -> None:
        self._fills.append(
            FillRow(
                id=new_id(),
                order_id=fill.order_id,
                run_id=self.run_id,
                ts=fill.ts,
                pair=str(fill.pair),
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
            )
        )
        await self._maybe_flush()

    async def record_ledger(self, entries: list[LedgerEntry]) -> None:
        for e in entries:
            self._ledger.append(
                LedgerEntryRow(
                    id=new_id(),
                    run_id=self.run_id,
                    ts=e.ts,
                    asset=e.asset,
                    amount=e.amount,
                    balance_after=e.balance_after,
                    reason=e.reason,
                    ref_id=e.ref_id,
                )
            )
        await self._maybe_flush()

    async def record_equity(self, ts: datetime, equity: Decimal, cash: Decimal, unrealized: Decimal) -> None:
        self._equity.append(
            EquitySnapshotRow(
                id=new_id(),
                run_id=self.run_id,
                ts=ts,
                equity=float(equity),
                cash=float(cash),
                unrealized_pnl=float(unrealized),
            )
        )
        await self._maybe_flush()

    async def finish(self, status: RunStatus, metrics: dict[str, Any] | None) -> None:
        await self.flush()
        async with self._sessionmaker() as session:
            row = await session.get(RunRow, self.run_id)
            if row is not None:
                row.status = status.value
                row.ended_at = utc_now()
                row.metrics = metrics
            await session.commit()

    async def _maybe_flush(self) -> None:
        if len(self._orders) + len(self._fills) + len(self._ledger) + len(self._equity) >= self._flush_every:
            await self.flush()

    async def flush(self) -> None:
        if not (self._orders or self._fills or self._ledger or self._equity):
            return
        # snapshot buffers; clear only after a successful commit so a
        # transient failure doesn't silently lose the audit trail
        orders, fills, ledger, equity = self._orders, self._fills, self._ledger, self._equity
        async with self._sessionmaker() as session:
            if orders:
                # upsert: orders are recorded at submit and again after fill;
                # dedupe by id, keeping the latest recorded state
                latest = {o.id: o for o in orders}
                stmt = pg_insert(OrderRow).values(
                    [
                        {c.name: getattr(o, c.name) for c in OrderRow.__table__.columns}
                        for o in latest.values()
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "status": stmt.excluded.status,
                        "filled_price": stmt.excluded.filled_price,
                        "filled_ts": stmt.excluded.filled_ts,
                        "fee": stmt.excluded.fee,
                    },
                )
                await session.execute(stmt)
            if fills:
                session.add_all(fills)
            if ledger:
                session.add_all(ledger)
            if equity:
                session.add_all(equity)
            await session.commit()
        self._orders = []
        self._fills = []
        self._ledger = []
        self._equity = []


@dataclass
class CompositeRecorder:
    """Tee: forwards every call to all children (DB + in-memory, e.g.)."""

    children: list[RunRecorder]
    run_id: RunId = field(init=False)

    def __post_init__(self) -> None:
        self.run_id = self.children[0].run_id

    async def start(self, info: RunInfo) -> None:
        for child in self.children:
            await child.start(info)

    async def record_order(self, order: Order) -> None:
        for child in self.children:
            await child.record_order(order)

    async def record_fill(self, fill: Fill) -> None:
        for child in self.children:
            await child.record_fill(fill)

    async def record_ledger(self, entries: list[LedgerEntry]) -> None:
        for child in self.children:
            await child.record_ledger(entries)

    async def record_equity(self, ts: datetime, equity: Decimal, cash: Decimal, unrealized: Decimal) -> None:
        for child in self.children:
            await child.record_equity(ts, equity, cash, unrealized)

    async def finish(self, status: RunStatus, metrics: dict[str, Any] | None) -> None:
        for child in self.children:
            await child.finish(status, metrics)


@dataclass
class InMemoryRecorder:
    """For tests and offline use: keeps everything in lists."""

    run_id: RunId = field(default_factory=lambda: RunId(new_id()))
    info: RunInfo | None = None
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    equity: list[tuple[datetime, Decimal, Decimal, Decimal]] = field(default_factory=list)
    final_status: RunStatus | None = None
    metrics: dict[str, Any] | None = None

    async def start(self, info: RunInfo) -> None:
        self.info = info

    async def record_order(self, order: Order) -> None:
        self.orders.append(order)

    async def record_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    async def record_ledger(self, entries: list[LedgerEntry]) -> None:
        self.ledger.extend(entries)

    async def record_equity(self, ts: datetime, equity: Decimal, cash: Decimal, unrealized: Decimal) -> None:
        self.equity.append((ts, equity, cash, unrealized))

    async def finish(self, status: RunStatus, metrics: dict[str, Any] | None) -> None:
        self.final_status = status
        self.metrics = metrics
