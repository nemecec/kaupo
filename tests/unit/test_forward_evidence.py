"""False-pass regression tests for the prospective evaluation contract."""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.db.models import EquitySnapshotRow, EventRow, FillRow, ForwardTrialRow, ResearchLedgerRow, RunRow
from kaupo.report.forward import ForwardPolicy, completed_positions, evaluate, frozen_config, signature

START = datetime(2026, 1, 1, tzinfo=UTC)


def make_run(id="root", **config):
    return RunRow(
        id=id,
        strategy_id="trend",
        strategy_version="source",
        mode="shadow",
        status="running",
        started_at=START - timedelta(minutes=1),
        config={
            "assignment_id": "slot",
            "pair": "BTC/EUR",
            "timeframe": "1d",
            "params": {},
            "fees": {"maker_bps": 40, "taker_bps": 80, "marketable_limit": "skip"},
            "risk": {"max_position_quote": 1000},
            "starting_cash": 10000,
            "behaviour_hash": "behaviour",
            "engine_version": "engine",
            **config,
        },
    )


def make_fill(id, side, size, day=0, run_id="root"):
    return FillRow(
        id=id,
        order_id=id,
        run_id=run_id,
        ts=START + timedelta(days=day),
        pair="BTC/EUR",
        side=side,
        price=1000,
        size=size,
        fee=0.4,
    )


def evidence():
    run = make_run()
    cfg = frozen_config(run)
    trial = ForwardTrialRow(
        id="trial",
        assignment_id="slot",
        registered_at=START,
        ends_at=START + timedelta(days=90),
        root_run_id=run.id,
        hypothesis="Frozen economic rationale",
        signature=signature(cfg),
        frozen_config=cfg,
        policy=ForwardPolicy().model_dump(),
        baseline_equity=10000,
    )
    points = [
        EquitySnapshotRow(
            id=f"p{i}",
            run_id="root",
            ts=START + timedelta(days=i),
            equity=10000 + i * 10 + (i % 3) * 5,
            cash=10000,
            unrealized_pnl=0,
        )
        for i in range(90)
    ]
    fills = [
        f
        for i in range(20)
        for f in (
            make_fill(f"b{i}", "buy", 1, day=i * 2),
            make_fill(f"s{i}", "sell", 1, day=i * 2 + 1),
        )
    ]
    costs = [
        ResearchLedgerRow(
            id="coverage",
            reference="statement",
            recorded_at=trial.ends_at,
            kind="coverage",
            amount_eur=0,
            period_start=START,
            period_end=trial.ends_at,
            note="checked invoices",
        )
    ]
    return trial, [run], points, fills, costs


def report(data, now=None):
    return evaluate(*data, now=now or START + timedelta(days=90))


def test_pass_only_requests_review_and_never_promotes():
    result = report(evidence())
    assert result["status"] == "review_required"
    assert result["completed_positions"] == 20
    assert result["automatic_live_promotion"] is False


def test_minimum_calendar_window_cannot_be_bypassed_with_many_trades():
    result = report(evidence(), START + timedelta(days=50))
    assert result["completed_positions"] == 20
    assert result["status"] == "collecting"


def test_backtest_metrics_and_pre_registration_trades_do_not_count():
    data = evidence()
    data[1][0].metrics = {"sharpe": 9, "num_round_trips": 900}
    for fill in data[3]:
        fill.ts -= timedelta(days=100)
    result = report(data)
    assert result["completed_positions"] == 0
    assert result["status"] == "invalidated"
    assert "pre-registration activity in forward ledger" in result["reasons"]


def test_partial_sells_count_as_one_position():
    fills = [make_fill("b", "buy", 1)]
    fills.extend(make_fill(f"s{i}", "sell", 0.1, day=i + 1) for i in range(10))
    assert completed_positions(fills) == (1, True)
    assert completed_positions(fills[:-1]) == (0, True)
    assert completed_positions([make_fill("s", "sell", 1)]) == (0, False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("fees", {"maker_bps": 16}),
        ("risk", {"max_position_quote": 5000}),
        ("params", {"fast": 5}),
        ("behaviour_hash", "new"),
        ("engine_version", "new"),
    ],
)
def test_changed_and_then_restored_configuration_invalidates(field, value):
    data = evidence()
    changed = make_run("changed", resumed_from="root", **{field: value})
    changed.started_at = START + timedelta(days=30)
    restored = make_run("restored", resumed_from="changed")
    restored.started_at = START + timedelta(days=60)
    data[1].extend([changed, restored])
    assert report(data)["status"] == "invalidated"


def test_documentation_only_strategy_version_does_not_invalidate():
    data = evidence()
    resumed = make_run("resumed", resumed_from="root", warmup=40)
    resumed.strategy_version = "new-file-hash-same-behaviour"
    resumed.started_at = START + timedelta(days=45)
    data[1].append(resumed)
    for point in data[2][45:]:
        point.run_id = "resumed"
    assert report(data)["status"] == "review_required"


def test_reset_ledger_does_not_get_rebased_into_a_pass():
    data = evidence()
    reset = make_run("reset")
    reset.started_at = START + timedelta(days=45)
    data[1].append(reset)
    assert "ledger continuity broken" in report(data)["reasons"]


def test_missing_cost_records_are_unknown_not_zero():
    data = evidence()
    data[4].clear()
    result = report(data)
    assert result["status"] == "insufficient_evidence"
    assert not result["cost_coverage_complete"]


def test_actual_research_costs_can_turn_a_trading_profit_into_a_loss():
    data = evidence()
    data[4].append(
        ResearchLedgerRow(
            id="bill",
            kind="cost",
            amount_eur=1000,
            period_start=START + timedelta(days=20),
            period_end=START + timedelta(days=20),
        )
    )
    result = report(data)
    assert result["trading_profit_eur"] > 0
    assert result["standalone_net_profit_eur"] < 0
    assert result["status"] == "insufficient_evidence"


def test_missing_middle_candles_fail_even_with_good_endpoints():
    data = evidence()
    del data[2][20:60]
    assert "incomplete equity coverage" in report(data)["reasons"]


def test_last_unclosed_candle_cannot_improve_results():
    data = evidence()
    result = report(data)
    data[2].append(
        EquitySnapshotRow(
            id="future",
            run_id="root",
            ts=START + timedelta(days=90),
            equity=1e8,
        )
    )
    assert report(data)["trading_profit_eur"] == result["trading_profit_eur"]


def test_fixed_window_does_not_expand_until_a_later_win():
    data = evidence()
    assert report(data, START + timedelta(days=180)) == report(data)


def test_nonfinite_equity_fails_closed():
    data = evidence()
    data[2][30].equity = float("nan")
    assert report(data)["status"] == "invalidated"


def test_risk_halt_audit_event_invalidates_even_without_run_metrics():
    data = evidence()
    event = EventRow(
        id="halt",
        ts=START + timedelta(days=40),
        source="engine",
        level="warn",
        message="halted",
        data={"run_id": "root", "halt_reason": "daily loss"},
    )
    result = evaluate(*data, now=START + timedelta(days=90), events=[event])
    assert result["status"] == "invalidated"
    assert "run halted during evaluation" in result["reasons"]


def test_tiny_trades_do_not_satisfy_position_count_gate():
    data = evidence()
    for fill in data[3]:
        fill.size = 0.001
    result = report(data)
    assert result["completed_positions"] == 0
    assert result["status"] == "insufficient_evidence"


def test_watchdog_restart_preserves_continuous_evidence():
    from kaupo.core.recorder import WATCHDOG_HALT_REASON

    data = evidence()
    data[1][0].status = "halted"
    data[1][0].metrics = {"halt_reason": WATCHDOG_HALT_REASON}
    successor = make_run(id="next", resumed_from="root")
    successor.started_at = START + timedelta(days=45)
    data[1].append(successor)
    for point in data[2]:
        if point.ts >= successor.started_at:
            point.run_id = "next"
    assert report(data)["status"] == "review_required"
