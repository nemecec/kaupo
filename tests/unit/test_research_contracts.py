"""Research declarations and actual usage imports reject ambiguous evidence."""

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaupo.research.contracts import ExperimentIn


def spec():
    return {
        "reference": "attempt1",
        "strategy_ref": "a" * 40,
        "mandate": "trend",
        "hypothesis": "A fixed rule improves net return after all trading fees",
        "changed_factor": "One fixed entry threshold",
        "rejection_rule": "Reject below the unchanged control",
        "variants": [
            {"id": "control", "strategy": "sma-cross"},
            {"id": "candidate", "strategy": "sma-cross"},
        ],
        "exchange": "kraken",
        "pairs": ["SOL/EUR"],
        "timeframe": "1d",
        "start": "2025-01-01T00:00:00Z",
        "end": "2025-02-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"variants": [{"id": "candidate", "strategy": "x"}, {"id": "candidate", "strategy": "x"}]},
        {"start": "2025-01-01T01:00:00Z"},
        {"pairs": ["SOL/USD"]},
        {"end": "2099-01-01T00:00:00Z"},
        {"strategy_ref": "main"},
    ],
)
def test_contract_cannot_omit_control_or_fixed_identity(change):
    with pytest.raises(ValidationError):
        ExperimentIn.model_validate({**spec(), **change})


def importer():
    path = Path(__file__).resolve().parents[2] / "scripts/import_research_costs.py"
    module_spec = importlib.util.spec_from_file_location("cost_import", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_costs_require_actual_usage_and_explicit_conversion(tmp_path):
    path = tmp_path / "charges.csv"
    header = "reference,kind,occurred_at,amount,currency,eur_per_unit,source\n"
    path.write_text(header + "provider-row1,usage,2026-09-17T00:00:00Z,10.00,USD,0.90,billing export\n")
    rows = importer().records(path)
    assert rows[0]["amount_eur"] == "9.00"
    assert rows[0]["reference"] == "moonshot:provider-row1"
    path.write_text(path.read_text().replace(",usage,", ",deposit,"))
    with pytest.raises(ValueError, match="usage charges"):
        importer().records(path)


def test_costs_reject_naive_dates_and_duplicate_references(tmp_path):
    path = tmp_path / "charges.csv"
    header = "reference,kind,occurred_at,amount,currency,eur_per_unit,source\n"
    line = "provider-row1,usage,2026-09-17T00:00:00Z,10.00,EUR,1,billing export\n"
    path.write_text(header + line + line)
    with pytest.raises(ValueError, match="unique"):
        importer().records(path)
    path.write_text(header + line.replace("00:00:00Z", "00:00:00"))
    with pytest.raises(ValueError, match="timezone"):
        importer().records(path)
