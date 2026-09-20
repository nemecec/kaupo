"""Preregistered experiments and public-data exports for isolated cloud jobs."""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from kaupo.api.deps import Principal, get_principal, require_research
from kaupo.config import Settings, get_settings
from kaupo.core.provenance import engine_version
from kaupo.db.models import ResearchExperimentRow
from kaupo.db.session import get_session
from kaupo.domain import new_id, utc_now
from kaupo.research.contracts import ExperimentIn, ResultIn
from kaupo.research.dataset import snapshot

router = APIRouter(prefix="/api/v1/research/experiments", tags=["research"])


def describe(row: ResearchExperimentRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "registered_at": row.registered_at,
        "status": row.status,
        "manifest": row.manifest,
        "economics": row.economics,
        "dataset_sha256": row.dataset_sha256,
        "result": row.result,
        "completed_at": row.completed_at,
        "automatic_live_promotion": False,
        "result_authority": "researcher-reported; inspect cloud artifacts before review",
    }


@router.post("", status_code=201)
async def register(
    body: ExperimentIn,
    _: Annotated[Principal, Depends(require_research)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    await session.execute(select(func.pg_advisory_xact_lock(71420832)))
    existing = await session.scalar(
        select(ResearchExperimentRow)
        .options(defer(ResearchExperimentRow.dataset))
        .where(ResearchExperimentRow.reference == body.reference)
    )
    manifest = body.model_dump(mode="json")
    if existing:
        if existing.manifest != manifest:
            raise HTTPException(409, "reference already registered with a different contract")
        return describe(existing)
    active = await session.scalar(
        select(func.count())
        .select_from(ResearchExperimentRow)
        .where(ResearchExperimentRow.status.in_(("registered", "data_ready")))
    )
    if (active or 0) >= 6:
        raise HTTPException(429, "six experiments await results; report failures before starting more")
    if not re.fullmatch(r"ghcr.io/nemecec/kaupo:[0-9a-f]{40}", settings.research_image):
        raise HTTPException(503, "research requires an immutable nemecec/kaupo image tag")
    row = ResearchExperimentRow(
        id=new_id(),
        reference=body.reference,
        registered_at=utc_now(),
        manifest=manifest,
        status="registered",
        economics={
            "maker_bps": settings.default_maker_fee_bps,
            "taker_bps": settings.default_taker_fee_bps,
            "slippage_bps": settings.default_slippage_bps,
            "marketable_limit": "skip",
            "engine_version": engine_version(),
            "image": settings.research_image,
        },
    )
    session.add(row)
    await session.flush()
    return describe(row)


@router.get("")
async def listing(
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(ResearchExperimentRow)
            .options(defer(ResearchExperimentRow.dataset))
            .order_by(ResearchExperimentRow.registered_at.desc())
            .limit(max(1, min(limit, 500)))
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(ResearchExperimentRow))
    variants = await session.scalar(
        select(func.sum(func.json_array_length(ResearchExperimentRow.manifest["variants"])))
    )
    return {
        "registered_experiments_total": total,
        "registered_variants_total": variants or 0,
        "experiments": [describe(r) for r in rows],
    }


@router.get("/{experiment_id}")
async def detail(
    experiment_id: str,
    _: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await session.get(
        ResearchExperimentRow, experiment_id, options=[defer(ResearchExperimentRow.dataset)]
    )
    if row is None:
        raise HTTPException(404, "experiment not found")
    return describe(row)


@router.get("/{experiment_id}/dataset")
async def dataset(
    experiment_id: str,
    _: Annotated[Principal, Depends(require_research)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    try:
        row = await session.scalar(
            select(ResearchExperimentRow).where(ResearchExperimentRow.id == experiment_id).with_for_update()
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "40001":
            raise HTTPException(409, "dataset is being captured; retry this request") from exc
        raise
    if row is None:
        raise HTTPException(404, "experiment not found")
    if row.dataset is None:
        if row.result is not None:
            raise HTTPException(409, "experiment ended before a dataset was captured")
        try:
            row.dataset, row.dataset_sha256 = await snapshot(session, row)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        row.status = "data_ready"
        await session.flush()
    return Response(
        row.dataset, media_type="application/gzip", headers={"X-Dataset-SHA256": row.dataset_sha256 or ""}
    )


@router.post("/{experiment_id}/result")
async def report_result(
    experiment_id: str,
    body: ResultIn,
    _: Annotated[Principal, Depends(require_research)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await session.scalar(
        select(ResearchExperimentRow).where(ResearchExperimentRow.id == experiment_id).with_for_update()
    )
    if row is None:
        raise HTTPException(404, "experiment not found")
    result = body.model_dump(mode="json")
    if row.result is not None:
        if row.result != result:
            raise HTTPException(409, "reported results are immutable")
        return describe(row)
    declared = {v["id"] for v in row.manifest["variants"]}
    reported = [v.get("id") for v in body.outcomes]
    if any(not isinstance(value, str) for value in reported):
        raise HTTPException(422, "every result needs its declared variant id")
    if len(reported) != len(declared) or set(reported) != declared:
        raise HTTPException(422, "report every declared variant exactly once, including failures")
    if body.dataset_sha256 != (row.dataset_sha256 or "0" * 64):
        raise HTTPException(409, "dataset digest does not match the registered snapshot")
    for outcome in body.outcomes:
        if outcome.get("status") not in ("completed", "failed", "not_run"):
            raise HTTPException(422, "every variant needs completed, failed, or not_run status")
        if outcome["status"] == "completed" and row.dataset_sha256 is None:
            raise HTTPException(422, "a successful result requires a captured dataset")
    row.result, row.status, row.completed_at = result, "reported", utc_now()
    await session.flush()
    return describe(row)
