"""Shadow-run resume end-to-end against Postgres with scripted exchange clients.

A deploy or restart leaves the old run's row "running" (the process is
dead); the successor supersedes it and, when the config is unchanged,
resumes its final ledger state and chain clock.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.core.engine import STOPPED_EXTERNALLY
from kaupo.core.recorder import SUPERSEDED_HALT_REASON
from kaupo.core.runner import PortfolioShadowRequest, ShadowRequest, run_portfolio_shadow, run_shadow
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, EventRow, RunRow
from kaupo.db.session import get_sessionmaker, sm_scope
from kaupo.domain import Candle, Pair, Timeframe, new_id, utc_now
from kaupo.risk.manager import RiskConfig
from kaupo.sdk.loader import load_strategies

pytestmark = pytest.mark.integration

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
TF = Timeframe.H1

STRATEGY = """
from pydantic import BaseModel

from kaupo.domain import OrderIntent, Side
from kaupo.sdk.protocol import StrategyBase

class BuyerParams(BaseModel):
    size: float = 0.1

class Buyer(StrategyBase):
    id = "buyer"
    params_schema = BuyerParams
    def on_candle(self, ctx):
        if ctx.position().size == 0:
            return [OrderIntent(pair=ctx.candle.pair, side=Side.BUY, size=self.params.size, reason="enter")]
        return []
"""

PORTFOLIO_STRATEGY = """
from kaupo.domain import OrderIntent, Pair, Side
from kaupo.sdk.protocol import PortfolioStrategyBase

BTC = Pair.parse("BTC/EUR")

class Buyer(PortfolioStrategyBase):
    id = "port-buyer"
    def on_candle(self, ctx):
        if not ctx.positions():
            return [OrderIntent(pair=BTC, side=Side.BUY, size=0.1, reason="enter")]
        return []
"""


def candle(pair: Pair, ts: datetime, price: float) -> Candle:
    return Candle(
        pair=pair, timeframe=TF, ts=ts, open=price, high=price + 1, low=price - 1, close=price, volume=1.0
    )


def hourly_history(pair: Pair, end: datetime, price: float) -> list[Candle]:
    candles = [candle(pair, end - timedelta(hours=i), price) for i in reversed(range(120))]
    return sorted(candles, key=lambda c: c.ts)


class ScriptedClient:
    """Serves backfill pages, then scripted poll batches; sets stop at the end.

    Backfill is paginated explicitly and the backfill/poll switch flips when
    a backfill fetch finds the pages exhausted — the run's freshening loop
    can also end on ``since >= now`` (no empty final page), so keying the
    switch on an empty page can stick the client in backfill mode forever.
    """

    def __init__(self, history: list[Candle], poll_batches: list[list[Candle]], stop: asyncio.Event) -> None:
        self.pages = [history[i : i + 50] for i in range(0, len(history), 50)]
        self.poll_batches = poll_batches
        self.stop = stop
        self.in_backfill = True

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if self.in_backfill:
            if since is None:
                return []
            if not self.pages:
                self.in_backfill = False
                return []
            return self.pages.pop(0)
        if self.poll_batches:
            return self.poll_batches.pop(0)
        self.stop.set()
        return []


class ScriptedUniverseClient:
    """Serves per-pair backfill pages, then per-pair poll batches; sets stop at the end."""

    def __init__(
        self,
        history: dict[Pair, list[Candle]],
        poll_batches: dict[Pair, list[list[Candle]]],
        stop: asyncio.Event,
    ) -> None:
        self.pages = {
            pair: [candles[i : i + 50] for i in range(0, len(candles), 50)]
            for pair, candles in history.items()
        }
        self.poll_batches = {pair: list(batches) for pair, batches in poll_batches.items()}
        self.stop = stop
        self.in_backfill = set(history)
        self.stop_scheduled = False

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if pair in self.in_backfill:
            if since is None:
                return []
            if not self.pages[pair]:
                self.in_backfill.discard(pair)
                return []
            return self.pages[pair].pop(0)
        if self.poll_batches[pair]:
            return self.poll_batches[pair].pop(0)
        if all(not batches for batches in self.poll_batches.values()) and not self.stop_scheduled:
            # the pollers run ahead of the engine: give the engine a moment to
            # process the last joined step before the stop lands
            self.stop_scheduled = True
            asyncio.get_running_loop().call_later(0.5, self.stop.set)
        return []


class StopAfterClient(ScriptedClient):
    """Sets the stop event the moment the last poll batch is served.

    The engine then sees the stop at the top of the candle loop and halts
    "stopped externally" — the start-up/busy-phase graceful-stop shape
    (halted, empty metrics), as opposed to the stream-dried-up shape
    (completed) the plain ScriptedClient produces.
    """

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        batch = await super().fetch_candles(pair, timeframe, since=since, limit=limit)
        if not self.in_backfill and batch and not self.poll_batches:
            self.stop.set()
        return batch


class KillAfterClient(ScriptedClient):
    """Writes a global control kill command to the events table mid-run."""

    def __init__(
        self, history: list[Candle], poll_batches: list[list[Candle]], stop: asyncio.Event, kill_on_batch: int
    ) -> None:
        super().__init__(history, poll_batches, stop)
        self._kill_on_batch = kill_on_batch
        self._served = 0

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        batch = await super().fetch_candles(pair, timeframe, since=since, limit=limit)
        if not self.in_backfill and batch:
            self._served += 1
            if self._served == self._kill_on_batch:
                async with sm_scope(get_sessionmaker()) as session:
                    session.add(
                        EventRow(
                            id=new_id(),
                            ts=utc_now(),
                            level="warning",
                            source="control",
                            message="kill all runs",
                            data={"command": "kill"},
                        )
                    )
        return batch


async def _runs(session: AsyncSession) -> Sequence[RunRow]:
    # populate_existing: _mark_running leaves stale state in the identity map
    stmt = select(RunRow).order_by(RunRow.started_at).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().all()


async def _snapshots(session: AsyncSession, run_id: str) -> Sequence[EquitySnapshotRow]:
    return (
        (
            await session.execute(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.run_id == run_id)
                .order_by(EquitySnapshotRow.ts)
            )
        )
        .scalars()
        .all()
    )


async def _mark_running(session: AsyncSession, run_id: str) -> None:
    """Leave the row as a dead process (deploy, restart) would: still 'running'."""
    row = await session.get(RunRow, run_id)
    assert row is not None
    row.status = "running"
    row.ended_at = None
    row.metrics = None
    await session.commit()


async def _run_shadow(
    tmp_path: Path, client: ScriptedClient, params: dict | None = None, risk: RiskConfig | None = None
):  # type: ignore[no-untyped-def]
    strategy = load_strategies(tmp_path)["buyer"]
    stop = client.stop
    return await run_shadow(
        ShadowRequest(
            strategy=strategy,
            params=params or {},
            pair=BTC,
            timeframe=TF,
            warmup=50,
            poll_interval_seconds=0,
            risk=risk or RiskConfig(),
        ),
        get_sessionmaker(),
        client,  # type: ignore[arg-type]
        stop=stop,
    )


async def _halt_reasons(session: AsyncSession, run_id: str) -> list[str]:
    """The halt reasons the run left in the audit log (record_halt event rows)."""
    stmt = select(EventRow).where(EventRow.source == "engine", EventRow.data["run_id"].as_string() == run_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [str((row.data or {}).get("halt_reason")) for row in rows]


async def test_superseded_run_resumes_ledger_state_and_chain(session: AsyncSession, tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    c1, c2, c3, c4 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(4))

    # run 1: enters on c1, fills on c2, then the "deploy" kills the process
    result1 = await _run_shadow(tmp_path, ScriptedClient(history, [[c1], [c2]], asyncio.Event()))
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    snaps1 = await _snapshots(session, run1.id)
    assert len(snaps1) == 2
    assert snaps1[-1].cash < 10_000  # the fill tied up cash
    assert snaps1[-1].unrealized_pnl != 0  # fee in the basis puts the position under water
    await _mark_running(session, run1.id)

    # run 2, same config: supersedes run 1 and resumes its state
    result2 = await _run_shadow(tmp_path, ScriptedClient([*history, c1, c2], [[c3]], asyncio.Event()))
    assert result2.num_fills == 0  # the carried position suppresses the strategy's entry
    run1, run2 = await _runs(session)
    assert run1.status == "halted"
    assert run1.metrics["halt_reason"] == SUPERSEDED_HALT_REASON
    assert run2.config["resumed_from"] == run1.id
    assert run2.config["chain_started_at"] == run1.started_at.isoformat()
    snaps2 = await _snapshots(session, run2.id)
    assert len(snaps2) == 1
    # the first snapshot continues exactly where the predecessor left off
    assert snaps2[0].cash == snaps1[-1].cash
    assert snaps2[0].unrealized_pnl == snaps1[-1].unrealized_pnl
    assert snaps2[0].equity == snaps1[-1].equity
    await _mark_running(session, run2.id)

    # run 3: the chain clock still starts at run 1
    result3 = await _run_shadow(tmp_path, ScriptedClient([*history, c1, c2, c3], [[c4]], asyncio.Event()))
    assert result3.num_fills == 0
    _, run2, run3 = await _runs(session)
    assert run2.metrics["halt_reason"] == SUPERSEDED_HALT_REASON
    assert run3.config["resumed_from"] == run2.id
    assert run3.config["chain_started_at"] == run1.started_at.isoformat()
    snaps3 = await _snapshots(session, run3.id)
    assert len(snaps3) == 1
    assert snaps3[0].cash == snaps2[0].cash
    assert snaps3[0].equity == snaps2[0].equity


async def test_gracefully_stopped_run_resumes(session: AsyncSession, tmp_path: Path) -> None:
    """A shadow run unwound to completed by the stop event (a graceful deploy
    or shutdown) is resumed by its successor, same as a superseded one."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    c1, c2, c3 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(3))

    # run 1: the stop event lands while the engine waits for the next candle,
    # so the stream dries up and the run unwinds to completed (no halt reason)
    result1 = await _run_shadow(tmp_path, ScriptedClient(history, [[c1], [c2]], asyncio.Event()))
    assert result1.status.value == "completed"
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    assert run1.status == "completed"
    assert run1.metrics is None
    snaps1 = await _snapshots(session, run1.id)
    assert snaps1[-1].cash < 10_000  # the fill tied up cash

    # run 2, same config: resumes run 1 without superseding it
    result2 = await _run_shadow(tmp_path, ScriptedClient([*history, c1, c2], [[c3]], asyncio.Event()))
    assert result2.num_fills == 0  # the carried position suppresses the strategy's entry
    run1, run2 = await _runs(session)
    assert run1.status == "completed"  # untouched: the slot was free
    assert run2.config["resumed_from"] == run1.id
    assert run2.config["chain_started_at"] == run1.started_at.isoformat()
    snaps2 = await _snapshots(session, run2.id)
    assert len(snaps2) == 1
    assert snaps2[0].cash == snaps1[-1].cash
    assert snaps2[0].unrealized_pnl == snaps1[-1].unrealized_pnl
    assert snaps2[0].equity == snaps1[-1].equity


async def test_config_changed_successor_starts_fresh(session: AsyncSession, tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    c1, c2, c3, c4 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(4))

    result1 = await _run_shadow(tmp_path, ScriptedClient(history, [[c1], [c2]], asyncio.Event()))
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    await _mark_running(session, run1.id)

    # same strategy and pair, new params: the superseded run is NOT resumed
    result2 = await _run_shadow(
        tmp_path, ScriptedClient([*history, c1, c2], [[c3], [c4]], asyncio.Event()), params={"size": 0.2}
    )
    assert result2.num_fills == 1  # a flat book: the strategy enters again
    run1, run2 = await _runs(session)
    assert run1.metrics["halt_reason"] == SUPERSEDED_HALT_REASON
    assert "resumed_from" not in run2.config
    assert "chain_started_at" not in run2.config
    snaps2 = await _snapshots(session, run2.id)
    assert snaps2[0].cash == 10_000.0  # fresh ledger at starting_cash
    assert snaps2[0].unrealized_pnl == 0.0


async def test_portfolio_superseded_run_resumes_ledger_state(session: AsyncSession, tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = {
        BTC: hourly_history(BTC, now - timedelta(hours=2), 100.0),
        SOL: hourly_history(SOL, now - timedelta(hours=2), 200.0),
    }
    for candles in history.values():
        await upsert_candles(session, candles)
    await session.commit()
    (tmp_path / "buyer.py").write_text(PORTFOLIO_STRATEGY)

    def step(i: int) -> dict[Pair, Candle]:
        ts = now + timedelta(hours=i - 1)
        return {BTC: candle(BTC, ts, 100.0), SOL: candle(SOL, ts, 200.0)}

    async def run_portfolio(poll: dict[Pair, list[list[Candle]]], hist: dict[Pair, list[Candle]]):  # type: ignore[no-untyped-def]
        strategy = load_strategies(tmp_path)["port-buyer"]
        client = ScriptedUniverseClient(hist, poll, asyncio.Event())
        return await run_portfolio_shadow(
            PortfolioShadowRequest(
                strategy=strategy,
                params={},
                pairs=[BTC, SOL],
                timeframe=TF,
                warmup=50,
                poll_interval_seconds=0,
            ),
            get_sessionmaker(),
            client,  # type: ignore[arg-type]
            stop=client.stop,
        )

    # run 1: enters BTC on step 1, fills on step 2
    result1 = await run_portfolio(
        {BTC: [[step(1)[BTC]], [step(2)[BTC]]], SOL: [[step(1)[SOL]], [step(2)[SOL]]]}, history
    )
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    snaps1 = await _snapshots(session, run1.id)
    assert len(snaps1) == 2
    assert snaps1[-1].cash < 10_000
    await _mark_running(session, run1.id)

    # run 2, same universe: resumes the chain
    hist2 = {pair: history[pair] + [step(i)[pair] for i in (1, 2)] for pair in (BTC, SOL)}
    result2 = await run_portfolio({BTC: [[step(3)[BTC]]], SOL: [[step(3)[SOL]]]}, hist2)
    assert result2.num_fills == 0  # the carried BTC position suppresses the entry
    run1, run2 = await _runs(session)
    assert run1.metrics["halt_reason"] == SUPERSEDED_HALT_REASON
    assert run2.config["resumed_from"] == run1.id
    assert run2.config["chain_started_at"] == run1.started_at.isoformat()
    assert run2.config["pair"] == "BTC/EUR,SOL/EUR"
    snaps2 = await _snapshots(session, run2.id)
    assert len(snaps2) == 1
    assert snaps2[0].cash == snaps1[-1].cash
    assert snaps2[0].unrealized_pnl == snaps1[-1].unrealized_pnl
    assert snaps2[0].equity == snaps1[-1].equity


async def test_shutdown_halted_run_resumes(session: AsyncSession, tmp_path: Path) -> None:
    """The start-up/busy-phase graceful stop (halted, empty metrics, reason
    "stopped externally" in the audit log) resumes, same as supersession."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    c1, c2, c3, c4 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(4))

    # run 1: fills on c2; the "deploy" stop lands as c3 arrives, so the
    # engine halts at the top of the candle loop
    result1 = await _run_shadow(tmp_path, StopAfterClient(history, [[c1], [c2], [c3]], asyncio.Event()))
    assert result1.status.value == "halted"
    assert result1.halt_reason == STOPPED_EXTERNALLY
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    assert run1.status == "halted"
    assert run1.metrics is None  # the row carries no reason...
    assert await _halt_reasons(session, run1.id) == [STOPPED_EXTERNALLY]  # ...the audit log does
    snaps1 = await _snapshots(session, run1.id)
    assert snaps1[-1].cash < 10_000

    # run 2, same config: resumes run 1 without superseding it
    result2 = await _run_shadow(tmp_path, ScriptedClient([*history, c1, c2], [[c4]], asyncio.Event()))
    assert result2.num_fills == 0  # the carried position suppresses the strategy's entry
    run1, run2 = await _runs(session)
    assert run1.status == "halted"  # untouched: the slot was free
    assert run2.config["resumed_from"] == run1.id
    assert run2.config["chain_started_at"] == run1.started_at.isoformat()
    snaps2 = await _snapshots(session, run2.id)
    assert len(snaps2) == 1
    assert snaps2[0].cash == snaps1[-1].cash
    assert snaps2[0].unrealized_pnl == snaps1[-1].unrealized_pnl
    assert snaps2[0].equity == snaps1[-1].equity


async def test_rail_halted_run_does_not_resume(session: AsyncSession, tmp_path: Path) -> None:
    """A genuine risk-rail halt (halted, empty metrics, accusing reason in
    the audit log) is a deliberate stop: the successor starts fresh."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # the rail trips only while c3's close stays on c1's clock day (the
    # engine clock ticks at candle close, so c3 closes at now+2h): anchor
    # away from midnight or the day rolls and the rail resets instead
    if now.hour >= 22:
        now -= timedelta(hours=2)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    tiny_loss_cap = RiskConfig(max_daily_loss_quote=1.0)
    c1, c2 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(2))
    c3 = candle(BTC, now + timedelta(hours=1), 90.0)  # the drop that trips the rail
    c4, c5 = (candle(BTC, now + timedelta(hours=i), 100.0) for i in (2, 3))

    # run 1: fills on c2, the price drop on c3 trips the daily-loss rail
    result1 = await _run_shadow(
        tmp_path, ScriptedClient(history, [[c1], [c2], [c3]], asyncio.Event()), risk=tiny_loss_cap
    )
    assert result1.status.value == "halted"
    assert result1.halt_reason.startswith("max daily loss hit")
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    assert run1.status == "halted"
    assert run1.metrics is None
    assert [r.startswith("max daily loss hit") for r in await _halt_reasons(session, run1.id)] == [True]

    # run 2, same config: NOT resumed — the rail halt was a deliberate stop
    result2 = await _run_shadow(
        tmp_path, ScriptedClient([*history, c1, c2, c3], [[c4], [c5]], asyncio.Event()), risk=tiny_loss_cap
    )
    assert result2.num_fills == 1  # a flat book: the strategy enters again
    _, run2 = await _runs(session)
    assert "resumed_from" not in run2.config
    assert "chain_started_at" not in run2.config
    snaps2 = await _snapshots(session, run2.id)
    assert snaps2[0].cash == 10_000.0  # fresh ledger at starting_cash
    assert snaps2[0].unrealized_pnl == 0.0


async def test_control_killed_run_does_not_resume(session: AsyncSession, tmp_path: Path) -> None:
    """A control kill (halted, empty metrics, "killed via control API" in the
    audit log) is a deliberate stop: the successor starts fresh."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = hourly_history(BTC, now - timedelta(hours=2), 100.0)
    await upsert_candles(session, history)
    await session.commit()
    (tmp_path / "buyer.py").write_text(STRATEGY)
    c1, c2, c3, c4 = (candle(BTC, now + timedelta(hours=i - 1), 100.0) for i in range(4))

    # run 1: a global kill command lands before c2 is processed; the fill
    # still happens (fills precede the control probe in the candle loop)
    result1 = await _run_shadow(
        tmp_path, KillAfterClient(history, [[c1], [c2]], asyncio.Event(), kill_on_batch=2)
    )
    assert result1.status.value == "halted"
    assert result1.halt_reason == "killed via control API"
    assert result1.num_fills == 1
    run1 = (await _runs(session))[0]
    assert run1.status == "halted"
    assert run1.metrics is None
    assert await _halt_reasons(session, run1.id) == ["killed via control API"]

    # run 2, same config: NOT resumed; the stale global kill does not
    # assassinate the fresh run (its probe ignores pre-start commands)
    result2 = await _run_shadow(tmp_path, ScriptedClient([*history, c1, c2], [[c3], [c4]], asyncio.Event()))
    assert result2.num_fills == 1  # a flat book: the strategy enters again
    _, run2 = await _runs(session)
    assert "resumed_from" not in run2.config
    assert "chain_started_at" not in run2.config
    snaps2 = await _snapshots(session, run2.id)
    assert snaps2[0].cash == 10_000.0
    assert snaps2[0].unrealized_pnl == 0.0
