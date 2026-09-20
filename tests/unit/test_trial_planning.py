"""Planning the forward window from one reference backtest.

The reference says how often a configuration completes a substantial
position. It never says whether the configuration works: no number here
relaxes a forward gate, and a reference that cannot support a plan is
reported as unusable rather than rounded into one.
"""

from datetime import UTC, datetime, timedelta

import pytest

from kaupo.config import default_maker_bps, default_taker_bps
from kaupo.core.provenance import engine_version
from kaupo.db.models import FillRow, RunRow
from kaupo.report.forward import (
    PLAN_MAX_HORIZON_DAYS,
    PLAN_MIN_HORIZON_DAYS,
    ForwardPolicy,
    effective_risk,
    plan_match_defects,
    plan_matches_run,
    reference_defects,
    trial_plan,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
POLICY = ForwardPolicy()
# Both runs were launched above the default schedule, at 90 bps taker and 12
# bps slippage.
FEES = {"taker_bps": 90.0, "maker_bps": 45.0, "slippage_bps": 12.0, "marketable_limit": "skip"}
# The shape a backtest stores: the risk configuration after the venue's fee
# and slippage were folded into it (kaupo/backtest/run.py).
REFERENCE_RISK = {
    "max_position_quote": 1000,
    "cooldown_candles": 12,
    "taker_fee_bps": 90.0,
    "slippage_bps": 12.0,
}
# The shape a shadow run stores for the same limits: the risk configuration as
# requested, with both fields still on their dataclass defaults, because
# run_shadow folds the venue's rates in only when it builds the risk manager.
SHADOW_RISK = {**REFERENCE_RISK, "taker_fee_bps": default_taker_bps(), "slippage_bps": 5.0}


def reference_run(run_id="ref", status="completed", metrics=None, days=365, **config):
    return RunRow(
        id=run_id,
        mode="backtest",
        strategy_id="trend",
        strategy_version="v1",
        status=status,
        started_at=START,
        ended_at=START + timedelta(days=days),
        metrics=metrics,
        config={
            "pair": "BTC/EUR",
            "timeframe": "1d",
            "exchange": "kraken",
            "instrument": "spot",
            "params": {"fast": 10},
            "start": START.isoformat(),
            "end": (START + timedelta(days=days)).isoformat(),
            "starting_cash": 10_000.0,
            "fees": dict(FEES),
            "risk": dict(REFERENCE_RISK),
            "lookback": 300,
            "liquidate_end": True,
            **config,
        },
    )


def shadow_run(strategy_version="v1", **config):
    """A fresh shadow run, recorded the way ``run_shadow`` records one."""
    return RunRow(
        id="root",
        mode="shadow",
        strategy_id="trend",
        strategy_version=strategy_version,
        status="running",
        started_at=START,
        config={
            "pair": "BTC/EUR",
            "timeframe": "1d",
            "params": {"fast": 10},
            "starting_cash": 10_000.0,
            # a shadow run always stamps one; a backtest never does
            "behaviour_hash": "behaviour-of-v1",
            "engine_version": engine_version(),
            "fees": dict(FEES),
            "risk": dict(SHADOW_RISK),
            "lookback": 300,
            "warmup": 300,
            **config,
        },
    )


def round_trips(count, days=365, size=1.0, price=1000.0):
    stride = max(2, (days - 2) // max(count, 1))
    return [
        FillRow(
            id=f"{side}{j}",
            order_id=f"{side}{j}",
            run_id="ref",
            ts=START + timedelta(days=j * stride + offset),
            pair="BTC/EUR",
            side=side,
            price=price,
            size=size,
            fee=0.8,
        )
        for j in range(count)
        for offset, side in ((0, "buy"), (1, "sell"))
    ]


def equity_window(days=365, snapshots=None):
    """First and last daily snapshot, and how many were recorded."""
    return (START, START + timedelta(days=days - 1), days if snapshots is None else snapshots)


def plan(count=40, days=365, size=1.0, run=None, snapshots=None):
    return trial_plan(
        run or reference_run(days=days),
        round_trips(count, days=days, size=size),
        equity_window(days, snapshots),
        POLICY,
    )


def test_horizon_is_the_margined_time_to_twenty_positions():
    result = plan(count=40)
    assert result["window_days"] == 365
    assert result["reference_completed_positions"] == 40
    assert round(result["positions_per_year"]) == 40
    assert result["days_for_min_positions"] == pytest.approx(182.5)
    assert result["required_horizon_days"] == 274  # 182.5 days, 1.5x margin
    assert result["horizon_days"] == PLAN_MIN_HORIZON_DAYS  # the floor is higher
    assert result["blockers"] == []
    assert result["usable"] is True


def test_a_slower_strategy_is_given_a_longer_window():
    result = plan(count=10)
    assert result["horizon_days"] == 1095
    assert result["horizon_days"] > plan(count=40)["horizon_days"]


def test_a_fast_strategy_still_runs_a_full_year():
    result = plan(count=200)
    assert result["required_horizon_days"] == 55
    assert result["horizon_days"] == 365


def test_a_horizon_beyond_the_cap_reports_unreachable_evidence():
    """The answer is that the evidence is out of reach, never "trade bigger"."""
    result = plan(count=5)
    assert result["required_horizon_days"] > PLAN_MAX_HORIZON_DAYS
    assert result["horizon_days"] is None
    assert result["usable"] is False
    assert any("enough forward evidence in a testable horizon" in reason for reason in result["blockers"])
    assert not any("larger position" in reason for reason in result["blockers"])


def test_small_positions_do_not_shorten_the_window():
    """100 EUR round trips are not the positions the gate counts."""
    result = plan(count=40, size=0.1)
    assert result["reference_completed_positions"] == 0
    assert result["horizon_days"] is None
    assert any("at least 5 are needed" in reason for reason in result["blockers"])


def test_too_few_reference_positions_cannot_estimate_a_rate():
    assert any("at least 5 are needed" in reason for reason in plan(count=4)["blockers"])


def test_a_short_reference_cannot_estimate_a_rate():
    result = plan(count=20, days=100)
    assert any("estimate a trade rate" in reason for reason in result["blockers"])
    assert result["usable"] is False


def test_gaps_in_the_equity_record_are_not_a_measured_window():
    result = plan(count=40, snapshots=200)
    assert "reference backtest has gaps in its equity record" in result["blockers"]


def test_a_truncated_backtest_is_not_the_window_it_requested():
    run = reference_run(end=(START + timedelta(days=900)).isoformat())
    result = plan(count=40, run=run)
    assert "reference backtest covers less of the market than it requested" in result["blockers"]


def test_a_broken_fill_ledger_is_not_a_rate():
    sells = [f for f in round_trips(40) if f.side == "sell"]
    result = trial_plan(reference_run(), sells, equity_window(), POLICY)
    assert "reference fill ledger is not a valid flat-to-flat sequence" in result["blockers"]
    assert result["horizon_days"] is None


def test_a_reference_without_equity_history_is_unusable():
    result = trial_plan(reference_run(), round_trips(40), None, POLICY)
    assert "reference backtest recorded no equity history" in result["blockers"]
    assert result["horizon_days"] is None


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"mode": "shadow"}, "must be a backtest run"),
        ({"status": "running"}, "did not complete"),
        ({"metrics": {"halt_reason": "daily loss"}}, "halted before the end"),
        ({"instrument": "perp"}, "must be a spot backtest"),
        ({"sweep": {"group": "g", "point": 2}}, "not one sweep slice"),
        ({"stability": {"group": "g", "window": 1, "of": 4}}, "not one stability slice"),
        ({"rolling_origin": {"period": "2026-W35"}}, "not one rolling_origin slice"),
        ({"starting_cash": 50_000.0}, "10000 EUR baseline"),
        ({"pair": "BTC/USD"}, "must trade EUR pairs"),
    ],
)
def test_reference_defects_name_what_is_wrong(changes, expected):
    run = reference_run()
    for key, value in changes.items():
        if hasattr(run, key):
            setattr(run, key, value)
        else:
            run.config[key] = value
    assert any(expected in defect for defect in reference_defects(run, POLICY))


@pytest.mark.parametrize(
    "fees,expected",
    [
        ({"taker_bps": 0, "maker_bps": 0, "marketable_limit": "skip"}, "positive maker and taker fees"),
        ({"marketable_limit": "skip"}, "positive maker and taker fees"),
        (
            {
                "taker_bps": default_taker_bps() / 2,
                "maker_bps": default_maker_bps() / 2,
                "marketable_limit": "skip",
            },
            "below the current schedule",
        ),
        (
            {
                "taker_bps": default_taker_bps(),
                "maker_bps": default_maker_bps(),
                "marketable_limit": "maker",
            },
            "live-mirror fill model",
        ),
    ],
)
def test_reference_must_be_priced_and_filled_like_the_trial(fees, expected):
    run = reference_run(fees=fees)
    assert any(expected in defect for defect in reference_defects(run, POLICY))


def test_a_clean_reference_has_no_defects():
    assert reference_defects(reference_run(), POLICY) == []


def test_plan_matches_only_the_configuration_it_measured():
    result = plan()
    shadow = shadow_run()
    assert plan_match_defects(result, shadow) == []
    assert plan_matches_run(result, shadow) is True
    shadow.config["params"] = {"fast": 11}
    assert plan_matches_run(result, shadow) is False
    shadow.config["params"] = {"fast": 10}
    shadow.strategy_id = "momentum"
    assert plan_matches_run(result, shadow) is False


def test_a_portfolio_reference_matches_only_the_same_universe():
    run = reference_run(pair="BTC/EUR,ETH/EUR", pairs=["BTC/EUR", "ETH/EUR"])
    result = plan(run=run)
    same = shadow_run(pair="BTC/EUR,ETH/EUR", pairs=["BTC/EUR", "ETH/EUR"])
    assert plan_matches_run(result, same) is True
    same.config["pairs"] = ["BTC/EUR", "SOL/EUR"]
    assert plan_matches_run(result, same) is False


def test_the_two_risk_representations_compare_after_the_fee_sync():
    """A backtest stores synced risk, a shadow run stores it unsynced."""
    reference, shadow = reference_run(), shadow_run()
    assert reference.config["risk"] != shadow.config["risk"]
    assert effective_risk(reference.config) == effective_risk(shadow.config)
    # the plan stores the limits the reference enforced, not the ones it was asked for
    assert plan()["reference_risk"] == {**REFERENCE_RISK, "taker_fee_bps": 90.0, "slippage_bps": 12.0}
    assert plan_match_defects(plan(), shadow) == []


def test_the_same_name_with_different_behaviour_is_not_the_same_reference():
    """Same strategy id and params, different code. The rate is not this run's."""
    result = plan(run=reference_run(behaviour_hash="behaviour-of-v1"))
    shadow = shadow_run(behaviour_hash="behaviour-of-v2")
    assert "reference measured different strategy behaviour" in plan_match_defects(result, shadow)


def test_behaviour_decides_before_the_source_version():
    """A docstring edit moves the source version but not the behaviour hash."""
    result = plan(run=reference_run(behaviour_hash="behaviour-of-v1"))
    shadow = shadow_run(strategy_version="v2", behaviour_hash="behaviour-of-v1")
    assert plan_match_defects(result, shadow) == []


def test_the_source_version_carries_a_reference_without_a_behaviour_hash():
    result = plan()  # a backtest records no behaviour hash
    assert plan_match_defects(result, shadow_run()) == []
    assert "reference measured a different strategy source version" in plan_match_defects(
        result, shadow_run(strategy_version="v2")
    )


def test_an_unrecorded_identity_is_a_mismatch_not_a_pass():
    run = reference_run()
    run.strategy_version = None
    result = plan(run=run)
    shadow = shadow_run(strategy_version=None)
    del shadow.config["behaviour_hash"]
    assert "reference or run records no strategy identity to compare" in plan_match_defects(result, shadow)


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"engine_version": "another-build"}, "computed under a different engine version"),
        ({"engine_version": ""}, "records no engine version"),
    ],
)
def test_a_plan_cannot_size_a_run_on_other_execution_code(config, expected):
    defects = plan_match_defects(plan(), shadow_run(**config))
    assert any(expected in defect for defect in defects)


def test_a_reference_that_recorded_another_engine_version_is_rejected():
    result = plan(run=reference_run(engine_version="older-build"))
    assert "reference ran under a different engine version" in plan_match_defects(result, shadow_run())


def test_a_reference_records_what_its_engine_version_cannot_prove():
    assert any("does not record the engine version" in note for note in plan()["limitations"])


@pytest.mark.parametrize(
    "fees,expected",
    [
        ({**FEES, "taker_bps": 120.0}, "priced taker_bps below this run"),
        ({**FEES, "maker_bps": 60.0}, "priced maker_bps below this run"),
        ({**FEES, "slippage_bps": 30.0}, "priced slippage_bps below this run"),
        ({"taker_bps": 90.0, "maker_bps": 45.0, "marketable_limit": "skip"}, "no slippage_bps to compare"),
        ({**FEES, "marketable_limit": "maker"}, "different fill model"),
    ],
)
def test_a_reference_cannot_size_a_run_it_priced_more_cheaply(fees, expected):
    assert expected in "; ".join(plan_match_defects(plan(), shadow_run(fees=fees)))


def test_a_more_expensive_reference_cannot_change_the_frequency_estimate():
    """Costs can change risk exits and entry frequency in either direction."""
    costly = reference_run(
        fees={**FEES, "taker_bps": 120.0},
        risk={**REFERENCE_RISK, "taker_fee_bps": 120.0},
    )
    assert "reference priced taker_bps above this run" in plan_match_defects(plan(run=costly), shadow_run())


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"risk": {**SHADOW_RISK, "max_position_quote": 4000}}, "different effective risk limits"),
        ({"risk": {**SHADOW_RISK, "cooldown_candles": 1}}, "different effective risk limits"),
        ({"risk": {}}, "no risk limits to compare"),
        ({"starting_cash": 25_000.0}, "different cash balance"),
    ],
)
def test_a_reference_must_share_the_limits_and_cash_that_size_positions(config, expected):
    assert expected in "; ".join(plan_match_defects(plan(), shadow_run(**config)))


def test_a_reference_on_another_venue_is_an_estimate_not_a_blocker():
    result = plan(run=reference_run(exchange="binance"))
    assert result["reference_exchange"] == "binance"
    assert result["execution_exchange"] == "kraken"
    assert result["rate_basis"] == "estimate"
    assert any("trade frequency" in warning for warning in result["warnings"])
    assert result["blockers"] == []  # advisory: the operator reads it and decides
    assert plan_match_defects(result, shadow_run()) == []


def test_a_same_venue_reference_carries_no_venue_warning():
    assert plan()["warnings"] == []


@pytest.mark.parametrize("missing", ["start", "end"])
def test_a_reference_without_a_requested_window_cannot_be_checked_for_truncation(missing):
    run = reference_run()
    del run.config[missing]
    result = plan(run=run)
    assert "reference backtest does not record the date window it requested" in result["blockers"]
    assert result["usable"] is False
    assert result["requested_days"] is None


def test_an_empty_requested_window_is_not_a_window():
    run = reference_run(end=START.isoformat())
    result = plan(run=run)
    assert "reference backtest requested an empty date window" in result["blockers"]
    assert result["usable"] is False


def test_api_release_does_not_change_the_reference_trading_engine_identity():
    result = plan(run=reference_run(engine_version="pinned-trading-build"))
    assert plan_match_defects(result, shadow_run(engine_version="pinned-trading-build")) == []
