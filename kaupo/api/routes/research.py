"""Immutable forward trials and operator-supplied research cost records."""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.api.deps import Principal, get_principal, require_admin, require_research
from kaupo.db.models import (
    EquitySnapshotRow,
    FillRow,
    ForwardTrialRow,
    OrderRow,
    ResearchLedgerRow,
    RunAssignmentRow,
    RunRow,
)
from kaupo.db.session import get_session
from kaupo.domain import new_id, utc_now
from kaupo.report.forward import (
    PLAN_MAX_HORIZON_DAYS,
    PLAN_MIN_HORIZON_DAYS,
    POLICY_VERSION,
    ForwardPolicy,
    aware,
    frozen_config,
    plan_match_defects,
    signature,
    trial_plan,
    trial_report,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])

# Operator-approved spending exception; UTC calendar month matches the ledger.
# This does not change any frozen forward-trial policy.
MONTHLY_BUDGET_OVERRIDES_EUR = {"2026-09": 200.0}


class TrialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=100)
    hypothesis: str = Field(min_length=20, max_length=4000)
    # The completed backtest that sizes the window. The server reads the
    # horizon off it, so a trial cannot be registered too short for its own
    # gate; the caller cannot name a horizon at all.
    reference_run_id: str = Field(min_length=1, max_length=32)


async def _plan_for(session: AsyncSession, reference_run_id: str, policy: ForwardPolicy) -> dict[str, Any]:
    run = await session.get(RunRow, reference_run_id)
    if run is None:
        raise HTTPException(404, "reference run not found")
    first, last, snapshots = (
        await session.execute(
            select(
                func.min(EquitySnapshotRow.ts),
                func.max(EquitySnapshotRow.ts),
                func.count(EquitySnapshotRow.id),
            ).where(EquitySnapshotRow.run_id == run.id)
        )
    ).one()
    window = (first, last, snapshots) if first is not None and last is not None else None
    fills = list((await session.scalars(select(FillRow).where(FillRow.run_id == run.id))).all())
    return trial_plan(run, fills, window, policy)


@router.get("/trial-plan")
async def plan_trial(
    reference_run_id: str,
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Advisory: how long a trial on the reference backtest's configuration must run.

    This endpoint changes nothing. It reports the trade rate the reference
    recorded, the window that rate implies for the position gate, and what
    stands in the way of using it. Registration applies the same rules.
    """
    policy = ForwardPolicy()
    return {
        "policy_version": POLICY_VERSION,
        "min_completed_positions": policy.min_completed_positions,
        "plan": await _plan_for(session, reference_run_id, policy),
    }


class CostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=200)
    kind: Literal["cost", "coverage"]
    amount_eur: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    period_start: AwareDatetime
    period_end: AwareDatetime
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def check_period(self) -> "CostIn":
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        if self.kind == "cost" and (self.period_start != self.period_end or self.amount_eur <= 0):
            raise ValueError("cost requires a positive EUR amount and one incurred timestamp")
        if self.kind == "coverage" and (self.period_start == self.period_end or self.amount_eur != 0):
            raise ValueError("coverage requires a nonempty period and zero amount")
        return self


@router.post("/trials", status_code=201)
async def register_trial(
    body: TrialIn,
    _: Annotated[Principal, Depends(require_research)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    # Serialize registrations for a slot. Old trials are never deleted.
    assignment = await session.scalar(
        select(RunAssignmentRow)
        .where(
            RunAssignmentRow.id == body.assignment_id,
        )
        .with_for_update()
    )
    if assignment is None:
        raise HTTPException(404, "assignment not found")
    if assignment.mode != "shadow" or not assignment.enabled:
        raise HTTPException(422, "an enabled shadow assignment is required")
    now = utc_now()
    active = await session.scalar(
        select(ForwardTrialRow.id).where(
            ForwardTrialRow.assignment_id == body.assignment_id,
            ForwardTrialRow.ends_at > now,
        )
    )
    if active:
        raise HTTPException(409, "this assignment already has an active trial")
    run = await session.scalar(
        select(RunRow)
        .where(
            RunRow.config["assignment_id"].as_string() == body.assignment_id,
            RunRow.mode == "shadow",
        )
        .order_by(RunRow.started_at.desc(), RunRow.id.desc())
        .limit(1)
    )
    if run is None or run.status != "running":
        raise HTTPException(422, "a running shadow run is required")
    cfg = run.config
    pending = await session.scalar(
        select(func.count())
        .select_from(OrderRow)
        .where(
            OrderRow.run_id == run.id,
            OrderRow.status == "open",
        )
    )
    if pending:
        raise HTTPException(422, "register before the strategy submits its first order")
    if cfg.get("resumed_from") or await session.scalar(
        select(func.count())
        .select_from(FillRow)
        .where(
            FillRow.run_id == run.id,
        )
    ):
        raise HTTPException(422, "register a fresh shadow assignment before its first fill")
    if not cfg.get("engine_version"):
        raise HTTPException(422, "run must record its trading engine version")
    if not cfg.get("fees") or not cfg.get("risk") or not cfg.get("behaviour_hash"):
        raise HTTPException(422, "run is missing execution provenance")
    if cfg.get("fees", {}).get("marketable_limit") != "skip":
        raise HTTPException(422, "trial requires the live-mirror skip fill model")
    pairs = cfg.get("pairs") or [cfg.get("pair", "")]
    if any(not pair.endswith("/EUR") for pair in pairs):
        raise HTTPException(422, "trial accounting currently requires EUR pairs")
    policy = ForwardPolicy()
    if cfg.get("starting_cash") != policy.capital_eur:
        raise HTTPException(422, "trial requires a 10000 EUR shadow baseline")
    if (
        run.strategy_id != assignment.strategy_id
        or cfg.get("params") != assignment.params
        or cfg.get("timeframe") != assignment.timeframe
        or cfg.get("pair") != assignment.pair
        or cfg.get("pairs") != assignment.pairs
        or (assignment.starting_cash is not None and cfg.get("starting_cash") != assignment.starting_cash)
    ):
        raise HTTPException(409, "assignment changes have not reached the running strategy")
    plan = await _plan_for(session, body.reference_run_id, policy)
    # First why the reference cannot size any trial, then why it cannot size
    # this one. The first answer stands on its own, so it reads better.
    if plan["blockers"]:
        raise HTTPException(422, "planning reference cannot size this trial: " + "; ".join(plan["blockers"]))
    mismatches = plan_match_defects(plan, run)
    if mismatches:
        raise HTTPException(
            422,
            "the planning reference does not describe this run: " + "; ".join(mismatches),
        )
    horizon = plan["horizon_days"]
    # The window a version 2 trial can hold, enforced at the boundary too.
    if not isinstance(horizon, int) or not PLAN_MIN_HORIZON_DAYS <= horizon <= PLAN_MAX_HORIZON_DAYS:
        raise HTTPException(422, "planning reference implies no usable evaluation window")
    # The window is fixed here, from the plan, and never moves afterwards.
    registered = ForwardPolicy(version=POLICY_VERSION, evaluation_days=horizon, plan=plan)
    frozen = frozen_config(run)
    trial = ForwardTrialRow(
        id=new_id(),
        assignment_id=body.assignment_id,
        registered_at=now,
        ends_at=now + timedelta(days=registered.evaluation_days),
        root_run_id=run.id,
        hypothesis=body.hypothesis,
        signature=signature(frozen),
        frozen_config=frozen,
        policy=registered.model_dump(),
        baseline_equity=registered.capital_eur,
    )
    session.add(trial)
    await session.flush()
    return await trial_report(session, trial, now)


@router.get("/trials")
async def list_trials(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(ForwardTrialRow)
        .order_by(
            ForwardTrialRow.registered_at.desc(),
            ForwardTrialRow.id.desc(),
        )
        .limit(max(1, min(limit, 500)))
    )
    return [
        {
            "id": r.id,
            "assignment_id": r.assignment_id,
            "registered_at": r.registered_at,
            "ends_at": r.ends_at,
            "hypothesis": r.hypothesis,
            "signature": r.signature,
        }
        for r in rows
    ]


@router.get("/trials/{trial_id}")
async def get_trial(
    trial_id: str,
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await session.get(ForwardTrialRow, trial_id)
    if row is None:
        raise HTTPException(404, "trial not found")
    return await trial_report(session, row, utc_now())


@router.post("/costs", status_code=201)
async def record_cost(
    body: CostIn,
    _: Annotated[Principal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    now = utc_now()
    if body.period_end > now:
        raise HTTPException(422, "future costs and coverage attestations are not permitted")
    row = ResearchLedgerRow(id=new_id(), recorded_at=now, **body.model_dump())
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "cost reference already recorded") from exc
    return {"id": row.id, **body.model_dump()}


@router.get("/budget")
async def research_budget(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    now = utc_now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = list(
        (
            await session.scalars(
                select(ResearchLedgerRow).where(
                    ResearchLedgerRow.period_end >= start,
                    ResearchLedgerRow.period_start <= now,
                )
            )
        ).all()
    )
    total = sum(float(r.amount_eur) for r in rows if r.kind == "cost")
    from kaupo.report.forward import costs_covered

    ceiling = MONTHLY_BUDGET_OVERRIDES_EUR.get(
        start.strftime("%Y-%m"), ForwardPolicy().monthly_research_budget_eur
    )
    covered = costs_covered(rows, aware(start), aware(now))
    return {
        "month": start.strftime("%Y-%m"),
        "budget_eur": ceiling,
        "recorded_spend_eur": total,
        "remaining_eur": max(0.0, ceiling - total) if covered else None,
        "actual_spend_eur": total if covered else None,
        "recorded_allowance_eur": max(0.0, ceiling - total),
        "accounting_status": "complete" if covered else "missing_usage_records",
        "over_budget": total > ceiling,
        "coverage_complete": covered,
        "provider_spend_enforced": False,
        "note": (
            "Uncovered periods have unknown costs, not zero costs. "
            "Prepaid balance is not usage. Hosting excluded."
        ),
    }


@router.get("/costs")
async def list_costs(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(ResearchLedgerRow)
        .order_by(
            ResearchLedgerRow.recorded_at.desc(),
            ResearchLedgerRow.id.desc(),
        )
        .limit(max(1, min(limit, 500)))
    )
    return [
        {
            "id": r.id,
            "reference": r.reference,
            "recorded_at": r.recorded_at,
            "kind": r.kind,
            "amount_eur": r.amount_eur,
            "period_start": r.period_start,
            "period_end": r.period_end,
            "note": r.note,
        }
        for r in rows
    ]
