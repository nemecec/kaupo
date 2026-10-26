"""Risk continuity uses recorded observations, not the restart wall clock."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kaupo.core.live_runner import _apply_recovered
from kaupo.domain import Fill, OrderId, OrderIntent, Pair, Position, Side
from kaupo.ledger.ledger import Ledger
from kaupo.risk.manager import Decision, RiskConfig, RiskManager, RiskState

FRIDAY = datetime(2026, 10, 23, 20, tzinfo=UTC)
PAIR = Pair.parse("BTC/EUR")


def state(ts=FRIDAY, cash=1000):
    return RiskState(ts, cash, {}, {}, cash)


@pytest.mark.parametrize("gap", [timedelta(hours=1), timedelta(days=3)])
def test_loss_baseline_survives_restart(gap):
    cfg = RiskConfig(max_daily_loss_quote=100)
    risk = RiskManager(cfg)
    assert risk.on_candle(state())
    assert risk.on_candle(state(FRIDAY + timedelta(minutes=30), 970))
    restored = RiskManager.restored(cfg, json.loads(json.dumps(risk.checkpoint())))
    assert restored.checkpoint() == risk.checkpoint()
    # Same day includes the first 30 loss; a new day starts at the last 970 floor.
    cash = 900 if gap.days == 0 else 870
    assert not restored.on_candle(state(FRIDAY + gap, cash))
    assert not risk.on_candle(state(FRIDAY + gap, cash))
    assert restored.checkpoint() == risk.checkpoint()


def test_loss_streak_cooldown_and_exits_survive_restart():
    cfg = RiskConfig(max_consecutive_losses=2, cooldown_candles=3)
    risk = RiskManager(cfg)
    risk.on_candle(state())
    risk.notify_trade_result(-1)
    resumed = RiskManager.restored(cfg, risk.checkpoint())
    resumed.notify_trade_result(-1)
    resumed = RiskManager.restored(cfg, resumed.checkpoint())
    assert resumed.checkpoint()["cooldown_remaining"] == 3
    positions = {PAIR: Position(PAIR, size=1, avg_entry=100)}
    observed = RiskState(FRIDAY + timedelta(days=3), 1000, positions, {PAIR: 100}, 1100)
    resumed.on_candle(observed)
    assessments = resumed.assess([OrderIntent(PAIR, Side.BUY, 1), OrderIntent(PAIR, Side.SELL, 1)], observed)
    assert [a.decision for a in assessments] == [Decision.REJECTED, Decision.APPROVED]
    assert resumed.checkpoint()["cooldown_remaining"] == 2  # closure does not consume candles


def test_new_request_config_does_not_erase_recorded_history():
    risk = RiskManager(RiskConfig(max_daily_loss_quote=200))
    risk.on_candle(state())
    resumed = RiskManager.restored(replace(risk.config, max_daily_loss_quote=50), risk.checkpoint())
    assert resumed.config.max_daily_loss_quote == 50
    assert not resumed.on_candle(state(FRIDAY + timedelta(hours=1), 940))
    assert RiskManager.restored(resumed.config, resumed.checkpoint()).halted


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"incomplete": True},
        {"last_floor": float("nan")},
        {"day_start_floor": float("inf")},
        {"cooldown_remaining": -1},
        {"consecutive_losses": True},
        {"day": "bad-date"},
        {"last_floor": None},
        {"halted": True, "halt_reason": ""},
    ],
)
def test_invalid_checkpoints_are_rejected(change):
    risk = RiskManager(RiskConfig())
    risk.on_candle(state())
    with pytest.raises(ValueError):
        RiskManager.restored(risk.config, {**risk.checkpoint(), **change})


def test_empty_or_unversioned_checkpoint_is_not_legacy():
    for checkpoint in ({}, {"day": None}):
        with pytest.raises(ValueError):
            RiskManager.restored(RiskConfig(), checkpoint)
    assert RiskManager.restored(RiskConfig()).checkpoint()["last_floor"] is None


def test_recovered_live_exit_counts_toward_loss_streak_without_resetting_floor():
    risk = RiskManager(RiskConfig(max_consecutive_losses=2))
    risk.on_candle(state())
    risk.notify_trade_result(-1)
    ledger = Ledger("EUR", 900, FRIDAY, positions={PAIR: Position(PAIR, size=1, avg_entry=100)})
    fill = Fill(OrderId("recovered"), PAIR, Side.SELL, FRIDAY + timedelta(hours=1), 90, 1, 0)
    _apply_recovered(ledger, [fill], risk)
    assert risk.checkpoint()["cooldown_remaining"] == risk.config.cooldown_candles
    assert risk.checkpoint()["last_floor"] == 1000
    assert float(ledger.cash) == 990
