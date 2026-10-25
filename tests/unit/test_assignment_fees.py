"""Assignment fee overrides reach every supervised runner without resetting identity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from kaupo.api.schemas import AssignmentIn, AssignmentUpdate
from kaupo.config import default_maker_bps, default_taker_bps
from kaupo.core import supervisor as sup
from kaupo.core.resume import config_hash
from kaupo.data.assignments import Assignment, effective_fees


def assignment(**changes):
    return replace(
        Assignment(
            id="a",
            strategy_id="s",
            pair="BTC/EUR",
            pairs=None,
            timeframe="1h",
            mode="shadow",
            params={},
            enabled=True,
            starting_cash=1000,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("taker_fee_bps", -1),
        ("taker_fee_bps", 10000),
        ("maker_fee_bps", -10000),
        ("maker_fee_bps", 10000),
        ("taker_fee_bps", float("nan")),
        ("maker_fee_bps", float("inf")),
    ],
)
@pytest.mark.parametrize("schema", [AssignmentIn, AssignmentUpdate])
def test_invalid_fee_overrides(schema, field, bad):
    kwargs = {"strategy_id": "s", "pair": "BTC/EUR", "timeframe": "1h"} if schema is AssignmentIn else {}
    with pytest.raises(ValidationError):
        schema(**kwargs, **{field: bad})


def test_zero_rebates_and_independent_defaults():
    assert effective_fees(assignment()) == (default_taker_bps(), default_maker_bps())
    assert effective_fees(assignment(taker_fee_bps=0, maker_fee_bps=-2)) == (0, -2)
    assert effective_fees(assignment(maker_fee_bps=-2)) == (default_taker_bps(), -2)
    assert AssignmentUpdate(maker_fee_bps=-2).maker_fee_bps == -2
    assert AssignmentUpdate().model_fields_set == set()
    assert AssignmentUpdate(maker_fee_bps=None).model_fields_set == {"maker_fee_bps"}


def test_fee_change_restarts_run_without_changing_resume_identity():
    base = assignment()
    changed = replace(base, maker_fee_bps=-2)
    first = sup.assignment_config_hash(base)
    second = sup.assignment_config_hash(changed)
    assert first != second
    assert sup.reconcile({"a": second}, {"a": first}, set()).stop == ["a"]
    assert sup.reconcile({"a": second}, {}, set()).start == ["a"]
    assert config_hash(base.strategy_id, base.pair, base.timeframe, base.params, base.pairs) == config_hash(
        changed.strategy_id,
        changed.pair,
        changed.timeframe,
        changed.params,
        changed.pairs,
    )
    assert sup.assignment_config_hash(replace(base, maker_fee_bps=default_maker_bps())) == first


@pytest.mark.parametrize(
    "mode,portfolio,target",
    [
        ("shadow", False, "run_shadow"),
        ("shadow", True, "run_portfolio_shadow"),
        ("live", False, "run_live"),
    ],
)
async def test_supervisor_passes_effective_fees(monkeypatch, mode, portfolio, target):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(sup, "KrakenClient", Client)
    monkeypatch.setattr(sup, "BinanceClient", Client)
    runner = AsyncMock(return_value="done")
    monkeypatch.setattr(sup, target, runner)
    row = assignment(
        mode=mode, pairs=["BTC/EUR", "SOL/EUR"] if portfolio else None, taker_fee_bps=0, maker_fee_bps=-2
    )
    await sup._run_assignment(row, {"s": object()}, None, asyncio.Event(), 20, 1800)
    request = runner.call_args.args[0]
    assert (request.taker_fee_bps, request.maker_fee_bps) == (0, -2)
