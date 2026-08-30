"""Shadow-run state carry: a superseded run's successor resumes its ledger.

Every deploy or supervisor restart starts a fresh process whose DbRecorder
supersedes the old run row ("superseded by a newer run of the same
strategy"). Without carry, the new run starts at starting_cash with zero
fills, so shadow-day accumulation and backtest-vs-shadow calibration reset
on every deploy.

Resume rule: a new run resumes when the latest ended run row of its slot
(the assignment row id when supervised, else strategy + pair) ended by
supersession or by a graceful stop, and runs the SAME config: the config
hash (strategy, pair or universe, timeframe, params) and the strategy
version must match. A graceful stop has two row shapes, by timing race: a
steady-state stop lets the candle stream dry up (status completed), a
start-up or busy-phase stop is seen at the top of the candle loop (status
halted, reason "stopped externally"). Both leave the run row's metrics
empty — engine-ended runs keep their halt reason in the events audit log
(record_halt), not in the row. That log is the discriminator: a halted
row with empty metrics resumes only when its recorded reason is "stopped
externally" (or absent — a process can die between the row write and the
audit write only on the shutdown path). A run halted by the risk rail,
killed via control, or failed does NOT resume — deliberate stops stay
stopped and the successor starts fresh at starting_cash.

What carries: cash and open positions only, rebuilt by replaying the whole
chain's recorded fills through a fresh Ledger — the same apply_fill that
accepted them live, so the carried state is exactly the proven accounting,
not a parallel computation. Pending orders, stop-loss/take-profit arms,
cooldowns, and risk state do NOT carry: limit orders expire within one
candle anyway, and protection re-arms from fresh candles on the successor.

Lineage: a resumed run's config gains "resumed_from" (the predecessor's run
id) and "chain_started_at" (the chain root's start, ISO format). The
chain's shadow clock (now - chain_started_at) is what the promotion gates
read. Metrics stay per-run; chain-level equity comes from stitch_equity.
"""

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.engine import STOPPED_EXTERNALLY
from kaupo.core.recorder import SUPERSEDED_HALT_REASON, supersede_stale_runs
from kaupo.db.models import EventRow, FillRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import Fill, OrderId, Pair, Position, RunMode, RunStatus, Side
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger

log = logging.getLogger(__name__)


def config_hash(
    strategy_id: str,
    pair: str,
    timeframe: str,
    params: dict[str, Any],
    pairs: list[str] | None = None,
) -> str:
    """Stable hash of the run-defining fields of an assignment.

    A change in strategy, pair, universe (``pairs``), timeframe, or params
    restarts the run; anything else (enabled flag, starting cash) does not.
    """
    canonical = json.dumps(
        {
            "strategy": strategy_id,
            "pair": pair,
            "pairs": pairs,
            "timeframe": timeframe,
            "params": params,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_config_hash(row: RunRow) -> str:
    """The config hash of a stored run row, from its config JSON."""
    config = row.config or {}
    return config_hash(
        row.strategy_id or "",
        str(config.get("pair", "")),
        str(config.get("timeframe", "")),
        config.get("params") or {},
        config.get("pairs"),
    )


def is_resumable(
    row: RunRow,
    *,
    new_config_hash: str,
    new_strategy_version: str,
    recorded_halt_reason: str | None = None,
) -> bool:
    """True when the run row is a resumable predecessor running the same config.

    Two endings qualify: a supersession (the row was still "running" when
    its successor claimed the slot) and a graceful stop — a shadow run
    never ends on its own (the candle stream is infinite), so an ending
    with no halt reason means the stop event unwound the run (deploy,
    shutdown, CLI stop). A graceful stop ends completed (stream dried up)
    or halted with empty metrics (stop seen at the candle loop's top). The
    latter shape is shared with rail halts and control kills, so it
    qualifies only when the audit log carries no accusation:
    ``recorded_halt_reason`` is the run's halt reason from the events
    table, and only "stopped externally" (or no record) is graceful.
    """
    if row.ended_at is None:
        return False
    halt_reason = (row.metrics or {}).get("halt_reason")
    superseded = row.status == RunStatus.HALTED.value and halt_reason == SUPERSEDED_HALT_REASON
    graceful_stop = (
        row.mode == RunMode.SHADOW.value
        and halt_reason is None
        and (
            row.status == RunStatus.COMPLETED.value
            or (row.status == RunStatus.HALTED.value and recorded_halt_reason in (None, STOPPED_EXTERNALLY))
        )
    )
    if not (superseded or graceful_stop):
        return False
    if row.strategy_version != new_strategy_version:
        return False
    return run_config_hash(row) == new_config_hash


def replay_fills(quote_asset: str, starting_cash: float, ts: datetime, fills: Iterable[Fill]) -> Ledger:
    """Rebuild the ledger state the fills produced: the carried accounting.

    The fills are applied through the same apply_fill that accepted them
    live, so the final cash and positions are exactly the recorded truth.
    """
    ledger = Ledger(quote_asset, starting_cash, ts)
    for fill in fills:
        ledger.apply_fill(fill)
    return ledger


@dataclass(frozen=True)
class ResumeState:
    """What a resumed run carries: the predecessor's final ledger state and chain root."""

    predecessor_run_id: str
    chain_started_at: str  # ISO timestamp of the chain root's start
    cash: Decimal
    positions: dict[Pair, Position]


async def _chain_rows(session: AsyncSession, tip: RunRow) -> list[RunRow] | None:
    """The resume chain ending at ``tip``, root first; None when the chain is broken."""
    chain = [tip]
    seen = {tip.id}
    row = tip
    while (parent_id := (row.config or {}).get("resumed_from")) is not None:
        if parent_id in seen:
            log.warning("Run chain at %s cycles; refusing to resume", tip.id)
            return None
        parent = await session.get(RunRow, parent_id)
        if parent is None:
            log.warning("Run %s resumes from %s, which is gone; refusing to resume", row.id, parent_id)
            return None
        chain.append(parent)
        seen.add(parent_id)
        row = parent
    chain.reverse()
    return chain


async def _recorded_halt_reason(session: AsyncSession, run_id: str) -> str | None:
    """The halt reason the run left in the audit log (record_halt); None when absent.

    Engine-ended runs keep their run row's metrics empty — the reason lives
    in the events table, the designed audit trail for halts.
    """
    row = (
        (
            await session.execute(
                select(EventRow)
                .where(EventRow.source == "engine", EventRow.data["run_id"].as_string() == run_id)
                .order_by(EventRow.ts.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    reason = (row.data or {}).get("halt_reason")
    return reason if isinstance(reason, str) else None


async def _load_fills(session: AsyncSession, run_ids: list[str]) -> list[Fill]:
    """All fills of the chain, oldest first.

    Fills within one candle share the candle's timestamp; ties break
    buys-before-sells ("buy" < "sell"), matching the venue's intra-candle
    order (entries execute before protection exits), then by id for
    determinism.
    """
    rows = (
        (
            await session.execute(
                select(FillRow)
                .where(FillRow.run_id.in_(run_ids))
                .order_by(FillRow.ts, FillRow.side, FillRow.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        Fill(
            order_id=OrderId(row.order_id),
            pair=Pair.parse(row.pair),
            side=Side(row.side),
            ts=row.ts,
            price=row.price,
            size=row.size,
            fee=row.fee,
        )
        for row in rows
    ]


async def prepare_resume(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    strategy_id: str,
    strategy_version: str,
    pair: str,  # config["pair"]: one pair, or the joined sorted universe
    pairs: list[str] | None,
    timeframe: str,
    params: dict[str, Any],
    quote_asset: str,
    assignment_id: str | None = None,
) -> ResumeState | None:
    """The carried state for a new shadow run; None for a fresh start.

    Claims the slot first (superseding stale "running" rows, exactly what
    DbRecorder.start does again when the run starts), so a predecessor
    orphaned by a dead process shows its supersession and can be resumed.
    """
    async with sm_scope(sessionmaker) as session:
        await supersede_stale_runs(
            session, mode=RunMode.SHADOW, strategy_id=strategy_id, pair=pair, timeframe=timeframe
        )
        stmt = (
            select(RunRow)
            .where(RunRow.mode == RunMode.SHADOW.value, RunRow.ended_at.is_not(None))
            .order_by(RunRow.started_at.desc())
            .limit(1)
        )
        if assignment_id is not None:
            stmt = stmt.where(RunRow.config["assignment_id"].as_string() == assignment_id)
        else:
            stmt = stmt.where(
                RunRow.strategy_id == strategy_id,
                RunRow.config["pair"].as_string() == pair,
            )
        predecessor = (await session.execute(stmt)).scalars().first()
        if predecessor is None:
            return None
        new_hash = config_hash(strategy_id, pair, timeframe, params, pairs)
        recorded_halt_reason: str | None = None
        metrics_reason = (predecessor.metrics or {}).get("halt_reason")
        if predecessor.status == RunStatus.HALTED.value and metrics_reason is None:
            # halted with empty metrics: a graceful stop, a rail halt, and a
            # control kill all write this row shape — only the audit log
            # tells them apart
            recorded_halt_reason = await _recorded_halt_reason(session, predecessor.id)
        if not is_resumable(
            predecessor,
            new_config_hash=new_hash,
            new_strategy_version=strategy_version,
            recorded_halt_reason=recorded_halt_reason,
        ):
            return None
        chain = await _chain_rows(session, predecessor)
        if chain is None:
            return None
        root = chain[0]
        starting_cash = (root.config or {}).get("starting_cash")
        if starting_cash is None:
            log.warning("Chain root run %s has no starting_cash in its config; starting fresh", root.id)
            return None
        fills = await _load_fills(session, [row.id for row in chain])
        try:
            ledger = replay_fills(quote_asset, float(starting_cash), root.started_at, fills)
        except (InsufficientFunds, InsufficientPosition):
            log.warning(
                "Replay of the run chain ending at %s failed its own accounting; starting fresh",
                predecessor.id,
                exc_info=True,
            )
            return None
        chain_started_at = (predecessor.config or {}).get("chain_started_at") or root.started_at.isoformat()
        log.info(
            "Resuming run chain from %s: cash %.2f %s, %d open position(s), chain started %s",
            predecessor.id,
            ledger.cash,
            quote_asset,
            len(ledger.open_positions),
            chain_started_at,
        )
        return ResumeState(
            predecessor_run_id=predecessor.id,
            chain_started_at=str(chain_started_at),
            cash=ledger.cash,
            positions=ledger.open_positions,
        )
