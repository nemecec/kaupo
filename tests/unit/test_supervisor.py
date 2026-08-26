"""Supervisor diff/reconcile logic: pure functions, no DB."""

from datetime import UTC, datetime, timedelta

from kaupo.core.supervisor import (
    EndKind,
    classify_end,
    config_hash,
    in_backoff,
    reconcile,
    resume_cleared,
)

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
