"""Currency inference and notification formatting fail closed on unknown units."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kaupo.db.models import RunRow
from kaupo.report.daily import _quote_currency


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"pair": "btc/eur"}, "EUR"),
        ({"pair": "BTC/USD,SOL/USD"}, "USD"),
        ({"pairs": ["BTC/EUR", "SOL/EUR"]}, "EUR"),
        ({"pair": "invalid"}, None),
        ({"pair": "BTC/"}, None),
        ({"pairs": []}, None),
        ({"pairs": "BTC/EUR"}, None),
        ({"pairs": ["BTC/EUR", None]}, None),
        ({"pairs": ["BTC/EUR", "SOL/USD"]}, None),
        ({}, None),
    ],
)
def test_quote_currency(config, expected):
    assert _quote_currency(RunRow(config=config)) == expected


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
@pytest.mark.parametrize(
    "groups,unknown,expected",
    [
        ({"USD": {"total_pnl": 5, "total_fees": 1}}, 0, "P&L 5 USD, fees 1 USD"),
        (
            {"EUR": {"total_pnl": 10, "total_fees": 2}, "USD": {"total_pnl": -3, "total_fees": 1}},
            0,
            "P&L 10 EUR, fees 2 EUR; P&L -3 USD, fees 1 USD",
        ),
        ({}, 1, "currency unavailable for 1 active runs"),
        (None, 0, "currency totals unavailable"),
    ],
)
def test_notification_preserves_currency(groups, unknown, expected):
    body = {
        "period": "2026-10-20",
        "totals": {
            "num_runs": 2,
            "active_runs": 2,
            "total_fills": 4,
            "total_pnl": 999,
            "total_fees": 999,
            "by_currency": groups,
            "unclassified_runs": unknown,
        },
    }
    script = Path(__file__).resolve().parents[2] / "deploy" / "daily-summary.jq"
    jq = shutil.which("jq")
    assert jq is not None
    result = subprocess.run(  # noqa: S603 — fixed local formatter; test data uses stdin
        [jq, "-r", "-f", str(script)],
        input=json.dumps(body),
        text=True,
        capture_output=True,
        check=True,
    )
    assert expected in result.stdout
    assert "999" not in result.stdout
    if groups is not None and "EUR" not in groups:
        assert "EUR" not in result.stdout
