import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from kaupo.cli import main as cli
from kaupo.venues import xstocks_order_status
from kaupo.venues.xstocks_expiry_check import check_expiry_request, check_order_expiry

ORDER = {
    "status": "expired",
    "descr": {"pair": "AAPLxUSD", "type": "buy", "ordertype": "limit"},
    "vol": "2",
    "vol_exec": "0.5",
    "expiretm": 200,
    "opentm": 100,
    "closetm": 200,
    "trades": ["trade-1"],
}


def check(order=None, start=201, end=202):
    return check_order_expiry(
        ORDER if order is None else order,
        expected_expiry=200,
        started_at=datetime.fromtimestamp(start, UTC),
        completed_at=datetime.fromtimestamp(end, UTC),
    )


@pytest.mark.parametrize(
    "state,closed", [("closed", 199), ("canceled", 199.9), ("expired", 200), ("closed", 200)]
)
def test_terminal_by_deadline_keeps_partial_fill_evidence(state, closed):
    order = {**deepcopy(ORDER), "status": state, "closetm": closed}
    before = deepcopy(order)
    report = check(order)
    assert report["status"] == "terminal_by_deadline"
    assert report["executed_volume"] == "0.5"
    assert report["deadline_source"] == "operator"
    assert order == before


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"expiretm": 0}, "expiry_mismatch"),
        ({"expiretm": 201}, "expiry_mismatch"),
        ({"closetm": 200.000001}, "terminal_after_deadline"),
        ({"closetm": 199}, "expired_before_deadline"),
        ({"closetm": 203}, "order_time_after_observation"),
        ({"opentm": 201, "closetm": 201}, "opened_after_deadline"),
        ({"opentm": 203, "closetm": 203}, "order_time_after_observation"),
    ],
)
def test_temporal_mismatch(changes, reason):
    report = check({**ORDER, **changes})
    assert report["status"] == "needs_review"
    assert report["reason"] == reason


@pytest.mark.parametrize("state", ["pending", "open"])
@pytest.mark.parametrize(
    "start,end,reason",
    [
        (201, 202, "active_after_deadline"),
        (199, 201, "observation_overlaps_deadline"),
        (200, 201, "observation_overlaps_deadline"),
        (199, 200, "observation_overlaps_deadline"),
        (198, 199.999999, "deadline_not_reached"),
    ],
)
def test_active_observation_interval(state, start, end, reason):
    report = check({**ORDER, "status": state}, start, end)
    assert report["status"] == "needs_review"
    assert report["reason"] == reason


def test_unsupported_order_type():
    report = check({**ORDER, "descr": {**ORDER["descr"], "ordertype": "market"}})
    assert report["reason"] == "unsupported_order_type"


@pytest.mark.parametrize(
    "field,value",
    [("expiretm", "NaN"), ("closetm", None), ("vol_exec", "3"), ("opentm", "1e100"), ("status", "unknown")],
)
def test_invalid_evidence_never_passes(field, value):
    assert check({**ORDER, field: value})["status"] == "needs_review"


def test_invalid_observation_interval():
    assert check(start=203, end=202)["reason"] == "invalid_observation_interval"
    report = check_order_expiry(
        ORDER, expected_expiry=200, started_at=datetime(2026, 1, 1), completed_at=datetime.now(UTC)
    )
    assert report["reason"] == "invalid_observation_interval"


@pytest.mark.parametrize(
    "ids,expiry",
    [
        ([], 200),
        (["a", "b"], 200),
        (["a"], 0),
        (["a"], -1),
        (["a"], True),
        (["a"], 1.5),
        (["a"], 253402300800),
    ],
)
def test_invalid_requests(ids, expiry):
    with pytest.raises(ValueError):
        check_expiry_request(ids, expiry)


@pytest.fixture
def reader(monkeypatch):
    read = AsyncMock(return_value={"one": ORDER})
    monkeypatch.setattr(xstocks_order_status, "read_order_status", read)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(kraken_api_key="k", kraken_api_secret="s"),  # noqa: S106
    )
    return read


def invoke(extra):
    return CliRunner().invoke(
        cli.app, ["run", "xstocks-order-status", "--units", "base", "--order-id", "one", *extra]
    )


@pytest.mark.parametrize("state,code", [("expired", 0), ("open", 1)])
def test_cli_emits_evidence_and_review_exit(reader, state, code):
    reader.return_value = {"one": {**ORDER, "status": state}}
    result = invoke(["--expected-expiry", "200"])
    assert result.exit_code == code, result.output
    body = json.loads(result.stdout)
    assert body["expiry_check"]["expected_expiry"] == 200
    assert body["orders"] == reader.return_value
    assert body["started_at"] <= body["completed_at"]


@pytest.mark.parametrize(
    "extra", [["--expected-expiry", "0"], ["--expected-expiry", "200", "--order-id", "two"]]
)
def test_cli_rejects_invalid_assertion_before_network(reader, extra):
    result = invoke(extra)
    assert result.exit_code == 2
    reader.assert_not_awaited()


def test_default_command_does_not_add_expiry_verdict(reader):
    result = invoke([])
    assert result.exit_code == 0
    assert "expiry_check" not in json.loads(result.stdout)
