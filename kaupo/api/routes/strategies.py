"""Strategy discovery endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from kaupo.api.deps import Principal, get_principal
from kaupo.config import Settings, get_settings
from kaupo.sdk.loader import load_strategies

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


@router.get("")
async def list_strategies(
    _: Annotated[Principal, Depends(get_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    loaded = load_strategies(settings.strategies_dir)
    return [
        {
            "id": s.id,
            "version": s.version,
            "params_schema": s.cls.params_schema.model_json_schema(),
        }
        for s in loaded.values()
    ]
