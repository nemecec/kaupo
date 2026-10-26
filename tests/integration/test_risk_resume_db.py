"""Persisted risk checkpoints stay aligned with recorder durability boundaries."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from kaupo.core.recorder import DbRecorder, RunInfo
from kaupo.core.resume import prepare_resume
from kaupo.db.models import RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import RunMode, RunStatus
from kaupo.risk.manager import RiskConfig, RiskManager, RiskState

pytestmark = pytest.mark.integration
FRIDAY = datetime(2026, 10, 23, 20, tzinfo=UTC)
CONFIG = {"pair": "BTC/EUR", "timeframe": "1h", "params": {}, "starting_cash": 1000}


async def start(mode=RunMode.SHADOW):
    recorder = DbRecorder(get_sessionmaker())
    await recorder.start(RunInfo(mode, "risk-test", "v1", "hash", CONFIG))
    return recorder


async def resume(mode=RunMode.SHADOW):
    return await prepare_resume(
        get_sessionmaker(),
        strategy_id="risk-test",
        strategy_version="v1",
        pair="BTC/EUR",
        pairs=None,
        timeframe="1h",
        params={},
        quote_asset="EUR",
        mode=mode,
    )


def observed():
    manager = RiskManager(RiskConfig(max_daily_loss_quote=100, max_consecutive_losses=2))
    manager.on_candle(RiskState(FRIDAY, 1000, {}, {}, 1000))
    manager.notify_trade_result(-1)
    return manager


@pytest.mark.parametrize("mode", [RunMode.SHADOW, RunMode.LIVE])
async def test_saved_checkpoint_survives_supersession_and_empty_successor(session, mode):
    recorder = await start(mode)
    manager = observed()
    await recorder.record_risk(manager.checkpoint())
    carried = await resume(mode)
    assert carried is not None
    assert carried.risk_checkpoint == manager.checkpoint()
    # An empty successor must carry the baseline forward before its first candle.
    successor = await start(mode)
    await successor.record_risk(carried.risk_checkpoint)
    await successor.finish(RunStatus.COMPLETED, None)
    next_carried = await resume(mode)
    assert next_carried is not None
    restored = RiskManager.restored(manager.config, next_carried.risk_checkpoint)
    restored.notify_trade_result(-1)
    assert restored.checkpoint()["cooldown_remaining"] == manager.config.cooldown_candles
    assert not restored.on_candle(RiskState(FRIDAY + timedelta(days=3), 850, {}, {}, 850))


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "incomplete": True},
        {"version": 99},
        None,
    ],
)
@pytest.mark.parametrize("status", ["running", "failed", "completed"])
async def test_missing_or_incomplete_modern_state_refuses_resume(session, payload, status):
    recorder = await start()
    await session.execute(
        update(RunRow)
        .where(RunRow.id == recorder.run_id)
        .values(risk_state=payload, status=status, ended_at=FRIDAY if status != "running" else None)
    )
    await session.commit()
    with pytest.raises(ValueError, match="risk checkpoint; resume refused"):
        await resume()


async def test_persisted_halt_is_not_bypassed_by_supersession(session):
    recorder = await start()
    manager = observed()
    manager.on_candle(RiskState(FRIDAY + timedelta(hours=1), 800, {}, {}, 800))
    await recorder.record_risk(manager.checkpoint())
    with pytest.raises(ValueError, match="persisted risk halt"):
        await resume()


async def test_legacy_rows_keep_explicit_compatibility_behavior(session):
    recorder = await start()
    await session.execute(
        update(RunRow)
        .where(RunRow.id == recorder.run_id)
        .values(
            config=CONFIG,
            risk_state=None,
        )
    )
    await session.commit()
    carried = await resume()
    assert carried is not None
    assert carried.risk_checkpoint is None


@pytest.mark.parametrize("commit_first", [False, True])
async def test_checkpoint_flush_retries_after_commit_failure(session, commit_first):
    recorder = await start()
    expected = observed().checkpoint()
    maker = get_sessionmaker()

    def failing_session():
        current = maker()
        commit = current.commit

        async def fail():
            if commit_first:
                await commit()
            raise RuntimeError("commit result unavailable")

        current.commit = fail
        return current

    recorder._sessionmaker = failing_session
    with pytest.raises(RuntimeError, match="commit result unavailable"):
        await recorder.record_risk(expected)
    assert recorder._risk_state == expected
    recorder._sessionmaker = maker
    await recorder.flush()
    row = await session.scalar(select(RunRow).where(RunRow.id == recorder.run_id))
    assert row.risk_state == expected
    assert recorder._risk_state is None
