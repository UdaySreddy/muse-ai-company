"""Model governance routes — automatic fetching of available models + reading/writing the default startup model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from aiteam.api import model_discovery
from aiteam.api.deps import get_scoped_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/models", tags=["model-governance"])


class DefaultModelBody(BaseModel):
    model: str = ""  # empty string = remove the key, restoring CC's own default


@router.get("/available")
async def available_models(force: bool = Query(False)) -> dict:
    """Available model list (file source of truth: models that have actually appeared in local transcripts)."""
    return {"success": True, "data": model_discovery.scan_available_models(force=force)}


@router.get("/usage")
async def model_usage(
    days: int = Query(7, ge=1, le=90),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Workflow agent usage aggregated by model tier over the last N days (orchestration charter observation caliber)."""
    usage = await repo.aggregate_model_usage(days)
    return {"success": True, "data": {"days": days, "usage": usage}}


@router.get("/default")
async def get_default_model() -> dict:
    """Current default startup model (the model key in ~/.claude/settings.json)."""
    return {"success": True, "data": {"model": model_discovery.read_default_model()}}


@router.put("/default")
async def put_default_model(body: DefaultModelBody) -> dict:
    """Set the default startup model (writes settings.json; takes effect in newly opened CC sessions)."""
    result = model_discovery.set_default_model(body.model)
    return {"success": bool(result.get("ok")), "data": result}
