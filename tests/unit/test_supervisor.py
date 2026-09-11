"""Supervisor diff/reconcile logic: pure functions, no DB."""

from datetime import UTC, datetime, timedelta

from kaupo.core.supervisor import (
    EndKind,
    classify_end,
    config_hash,
    in_backoff,
    reconcile,
    resume_cleared,
    staleness_reference,
    watchdog_is_stale,
    watchdog_stale_after,
)
from kaupo.domain import Timeframe

NOW = datetime(2026, 8, 25, tzinfo=UTC)


class TestConfigHash:
    def test_stable_regardless_of_param_order(self) -> None:
        a = config_hash("sma-cross", "BTC/EUR", "1h", {"fast": 10, "slow": 30})
        b = config_hash("sma-cross", "BTC/EUR", "1h", {"slow": 30, "fast": 10})
        assert a == b

    def test_params_change_detected(self) -> None:
        a = config_hash("sma-cross", "BTC/EUR", "1h", {"fast": 10})
        b = config_hash("sma-cross", "BTC/EUR", "1h", {"fast": 11})
        assert a != b

    def test_each_run_field_changes_the_hash(self) -> None:
        base = config_hash("sma-cross", "BTC/EUR", "1h", {})
        assert config_hash("other", "BTC/EUR", "1h", {}) != base
        assert config_hash("sma-cross", "ETH/EUR", "1h", {}) != base
        assert config_hash("sma-cross", "BTC/EUR", "4h", {}) != base

    def test_pairs_change_the_hash(self) -> None:
        base = config_hash("momentum-rotation", "BTC/EUR,SOL/EUR", "1h", {}, ["BTC/EUR", "SOL/EUR"])
        assert config_hash("momentum-rotation", "BTC/EUR,SOL/EUR", "1h", {}, ["BTC/EUR", "SOL/EUR"]) == base
        # a universe change restarts the run
        assert config_hash("momentum-rotation", "ADA/EUR,BTC/EUR", "1h", {}, ["ADA/EUR", "BTC/EUR"]) != base
        # single-pair and portfolio runs never collide
        assert config_hash("momentum-rotation", "BTC/EUR,SOL/EUR", "1h", {}) != base


class TestReconcile:
    def test_starts_a_missing_run(self) -> None:
        plan = reconcile({"a": "h1"}, {}, set())
        assert plan.start == ["a"]
        assert plan.stop == []

    def test_stops_a_run_without_a_desired_row(self) -> None:
        plan = reconcile({}, {"a": "h1"}, set())
        assert plan.stop == ["a"]
        assert plan.start == []

    def test_matching_run_is_left_alone(self) -> None:
        plan = reconcile({"a": "h1"}, {"a": "h1"}, set())
        assert plan.start == []
        assert plan.stop == []

    def test_hash_change_stops_first_and_starts_on_the_next_pass(self) -> None:
        # the old task is still live: stop it, do not start over it
        plan = reconcile({"a": "h2"}, {"a": "h1"}, set())
        assert plan.stop == ["a"]
        assert plan.start == []
        # next pass, after the stopped task was reaped
        plan = reconcile({"a": "h2"}, {}, set())
        assert plan.start == ["a"]
        assert plan.stop == []

    def test_held_down_rows_are_not_started(self) -> None:
        plan = reconcile({"a": "h1", "b": "h2"}, {}, {"b"})
        assert plan.start == ["a"]


class TestClassifyEnd:
    def test_stop_event_wins_over_everything(self) -> None:
        assert classify_end(True, False, None) is EndKind.STOPPED
        assert classify_end(True, True, "kill") is EndKind.STOPPED

    def test_failure_is_a_crash(self) -> None:
        assert classify_end(False, True, "kill") is EndKind.CRASHED

    def test_kill_stays_down(self) -> None:
        assert classify_end(False, False, "kill") is EndKind.KILLED

    def test_switch_is_a_deliberate_restart(self) -> None:
        assert classify_end(False, False, "switch") is EndKind.RESTART

    def test_any_other_end_is_a_crash(self) -> None:
        assert classify_end(False, False, None) is EndKind.CRASHED
        assert classify_end(False, False, "pause") is EndKind.CRASHED


class TestBackoff:
    def test_inside_and_outside_the_window(self) -> None:
        assert in_backoff(NOW, NOW + timedelta(seconds=59))
        assert not in_backoff(NOW, NOW + timedelta(seconds=60))
        assert not in_backoff(NOW, NOW + timedelta(minutes=5))


class TestResumeCleared:
    def test_resume_command_clears(self) -> None:
        assert resume_cleared(NOW, NOW - timedelta(hours=1), "resume")

    def test_row_update_clears(self) -> None:
        assert resume_cleared(NOW, NOW + timedelta(seconds=1), "kill")

    def test_stays_down_otherwise(self) -> None:
        assert not resume_cleared(NOW, NOW - timedelta(hours=1), "kill")
        assert not resume_cleared(NOW, NOW - timedelta(hours=1), None)


class TestWatchdog:
    def test_stale_after_scales_with_timeframe(self) -> None:
        assert watchdog_stale_after(Timeframe.H1) == timedelta(hours=2, minutes=10)
        assert watchdog_stale_after(Timeframe.H4) == timedelta(hours=8, minutes=10)
        assert watchdog_stale_after(Timeframe.D1) == timedelta(hours=48, minutes=10)

    def test_healthy_oscillation_is_not_stale(self) -> None:
        # the snapshot ts is the candle OPEN time and lands at close: a healthy
        # run's newest ts oscillates between 1x and 2x the timeframe behind
        assert not watchdog_is_stale(NOW - timedelta(hours=1), NOW, Timeframe.H1)
        assert not watchdog_is_stale(NOW - timedelta(hours=2), NOW, Timeframe.H1)
        assert not watchdog_is_stale(NOW - timedelta(hours=8), NOW, Timeframe.H4)

    def test_beyond_the_threshold_is_stale(self) -> None:
        # the 2026-08-31 stall shape: runs silent for hours beyond the cadence
        assert watchdog_is_stale(NOW - timedelta(hours=3), NOW, Timeframe.H1)
        assert watchdog_is_stale(NOW - timedelta(hours=9), NOW, Timeframe.H4)
        assert watchdog_is_stale(NOW - timedelta(hours=50), NOW, Timeframe.D1)

    def test_fresh_run_waiting_for_its_first_candle_is_not_stale(self) -> None:
        # a daily run started 12h ago has nothing to snapshot yet
        assert not watchdog_is_stale(NOW - timedelta(hours=12), NOW, Timeframe.D1)

    def test_a_starting_task_is_measured_from_its_own_birth(self) -> None:
        # the 2026-09-11 kill loop: the predecessor's last snapshot went stale
        # during an outage, and every startup attempt died at the first pass
        reference = staleness_reference(
            task_started_at=NOW - timedelta(seconds=15),
            row_started_at=NOW - timedelta(hours=20),
            last_snapshot_ts=NOW - timedelta(hours=9),
        )
        assert not watchdog_is_stale(reference, NOW, Timeframe.H4)

    def test_an_old_task_with_stale_snapshots_is_still_stale(self) -> None:
        # startup grace must not hide a run that wedged long after its birth
        reference = staleness_reference(
            task_started_at=NOW - timedelta(hours=20),
            row_started_at=NOW - timedelta(hours=20),
            last_snapshot_ts=NOW - timedelta(hours=9),
        )
        assert watchdog_is_stale(reference, NOW, Timeframe.H4)

    def test_reference_prefers_the_newest_input(self) -> None:
        assert staleness_reference(NOW, NOW - timedelta(hours=3), None) == NOW
        # own row: the snapshot is the progress signal, here newer than the row
        assert staleness_reference(
            NOW - timedelta(hours=3), NOW - timedelta(hours=2), NOW - timedelta(hours=1)
        ) == NOW - timedelta(hours=1)

    def test_a_fresh_row_does_not_hide_old_snapshots(self) -> None:
        # the stall signal lives in the snapshot, not the row's start time
        reference = staleness_reference(
            task_started_at=NOW - timedelta(seconds=5),
            row_started_at=NOW - timedelta(seconds=2),
            last_snapshot_ts=NOW - timedelta(hours=3),
        )
        assert watchdog_is_stale(reference, NOW, Timeframe.H1)

    def test_a_healthy_fresh_run_inside_the_band_is_not_stale(self) -> None:
        # a fresh run's first snapshot can open up to one timeframe before
        # its row time and still sit inside the healthy band
        reference = staleness_reference(
            task_started_at=NOW - timedelta(minutes=30),
            row_started_at=NOW - timedelta(minutes=29),
            last_snapshot_ts=NOW - timedelta(hours=1),
        )
        assert not watchdog_is_stale(reference, NOW, Timeframe.H1)
