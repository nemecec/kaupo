import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_order_fills, xstocks_order_status
from kaupo.venues.xstocks_fill_times import check_order_fill_times

ORDER = {
    "status": "expired",
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit"},
    "vol": "2",
    "vol_exec": "0.5",
    "expiretm": 200,
    "opentm": 100,
    "closetm": 200,
    "trades": ["t1"],
}
FILL = {
    "ordertxid": "o1",
    "pair": "AAPLSPVUSD",
    "type": "buy",
    "ordertype": "limit",
    "time": 150,
    "vol": "0.5",
    "price": "100",
    "cost": "50",
    "fee": "0",
}


def check(time=150, order=None, expiry=None):
    return check_order_fill_times(
        {"o1": ORDER if order is None else order}, {"t1": {**FILL, "time": time}}, expected_expiry=expiry
    )


@pytest.mark.parametrize("time", [100, "100.00000000000000000001", 150.5, 200])
def test_inclusive_lifetime_and_expiry(time):
    report = check(time, expiry=200)
    assert report["status"] == "times_match"
    assert report["deadline_source"] == "operator"
    assert report["orders"]["o1"]["fills"]["t1"]["time"] == time


@pytest.mark.parametrize(
    "time,violations",
    [
        ("99.99999999999999999999", ["fill_before_open"]),
        ("200.00000000000000000001", ["fill_after_close", "fill_after_expected_expiry"]),
    ],
)
def test_exact_boundary_violations(time, violations):
    report = check(time, expiry=200)
    assert report["status"] == "needs_review"
    assert report["orders"]["o1"]["fills"]["t1"]["violations"] == violations


@pytest.mark.parametrize("state", ["closed", "canceled", "expired"])
def test_terminal_states_check_close_even_without_expected_expiry(state):
    report = check(201, {**ORDER, "status": state})
    assert report["orders"]["o1"]["fills"]["t1"]["violations"] == ["fill_after_close"]
    assert "expected_expiry" not in report


@pytest.mark.parametrize("state", ["pending", "open"])
def test_active_order_uses_open_time_and_optional_deadline(state):
    order = {**ORDER, "status": state}
    del order["closetm"]
    assert check(201, order)["status"] == "times_match"
    report = check(201, order, 200)
    assert report["orders"]["o1"]["fills"]["t1"]["violations"] == ["fill_after_expected_expiry"]
    assert "closed_at" not in report["orders"]["o1"]


def test_expected_deadline_is_independent_of_recorded_expiry():
    report = check(199, expiry=198)
    assert report["orders"]["o1"]["fills"]["t1"]["violations"] == ["fill_after_expected_expiry"]


@pytest.mark.parametrize("time", [None, True, "NaN", float("inf"), "1e100", "bad"])
def test_invalid_timestamps_require_review(time):
    report = check(time)
    assert report["status"] == "needs_review"
    assert report["orders"]["o1"]["fills"]["t1"]["status"] == "unresolved"


def test_empty_fills_never_claim_timing_evidence():
    order = {**ORDER, "trades": [], "vol_exec": "0"}
    report = check_order_fill_times({"o1": order}, {}, expected_expiry=200)
    assert report["status"] == "no_fills"
    assert report["orders"]["o1"]["status"] == "no_fills"


def test_multiple_orders_keep_distinct_lifetimes_and_do_not_mutate():
    orders = {"o1": ORDER, "o2": {**ORDER, "opentm": 160, "trades": ["t2"]}}
    fills = {"t1": FILL, "t2": {**FILL, "ordertxid": "o2"}}
    before = deepcopy((orders, fills))
    report = check_order_fill_times(orders, fills)
    assert report["orders"]["o1"]["status"] == "times_match"
    assert report["orders"]["o2"]["status"] == "needs_review"
    assert (orders, fills) == before
    with pytest.raises(ValueError, match="exactly one"):
        check_order_fill_times(orders, fills, expected_expiry=200)


@pytest.mark.parametrize("fills", [{}, {"other": FILL}, {"t1": FILL, "extra": FILL}])
def test_requires_exact_references(fills):
    with pytest.raises(ValueError, match="exactly the referenced"):
        check_order_fill_times({"o1": ORDER}, fills)


def test_wrong_owner_is_unresolved():
    report = check_order_fill_times({"o1": ORDER}, {"t1": {**FILL, "ordertxid": "other"}})
    assert report["orders"]["o1"]["fills"]["t1"]["reason"] == "fill_order_mismatch"


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(xstocks_order_status, "read_order_status", AsyncMock(return_value={"o1": ORDER}))
    read = AsyncMock(return_value={"t1": FILL})
    monkeypatch.setattr(xstocks_order_fills, "read_order_fills", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return read


@pytest.mark.parametrize("time,code", [(150, 0), (201, 1), (99, 1)])
@pytest.mark.parametrize("expiry", [False, True])
def test_cli_volume_match_does_not_hide_timing_violation(reader, time, code, expiry):
    reader.return_value = {"t1": {**FILL, "time": time}}
    args = ["run", "xstocks-order-status", "--units", "base", "--order-id", "o1", "--check-fills"]
    if expiry:
        args += ["--expected-expiry", "200"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == code, result.output
    body = json.loads(result.stdout)
    assert body["fill_check"]["status"] == "volumes_match"
    assert body["fill_time_check"]["status"] == ("times_match" if code == 0 else "needs_review")
    assert body["fills"] == reader.return_value
    if expiry:
        assert body["expiry_check"]["status"] == "terminal_by_deadline"
