"""Knowledge layer P1b — unified search routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.unified_search import unified_search
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/search", tags=["unified-search"])


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=50),
    project_id: str = Query("", description="Limit to a project (empty = all)"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Three-arm RRF unified search: memo/report/task full text + reference graph + exact ID/title."""
    results = await unified_search(
        repo, q, limit=limit, project_id=project_id or None
    )
    return {"success": True, "data": results}
