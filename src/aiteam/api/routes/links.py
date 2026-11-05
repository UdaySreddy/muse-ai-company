"""Knowledge layer P1a — cross-domain reference graph query routes (docs/knowledge-layer-design.md)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/links", tags=["knowledge-links"])


@router.get("")
async def list_links(
    kind: str = Query(..., description="Endpoint type: task_memo/report/task/run/commit/memory"),
    id: str = Query(..., description="Endpoint ID"),
    direction: str = Query("both", pattern="^(in|out|both)$"),
    limit: int = Query(100, ge=1, le=500),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Look up reference edges by endpoint. in = who referenced it / out = who it referenced / both."""
    links = await repo.find_knowledge_links(kind, id, direction=direction, limit=limit)
    return {"success": True, "data": [lk.model_dump(mode="json") for lk in links]}


@router.get("/fanout")
async def link_fanout(
    kind: str = Query(...),
    id: str = Query(...),
    depth: int = Query(2, ge=1, le=2),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Undirected recursive fan-out: nodes reachable within depth ≤ 2 from the seed node (traceback chain)."""
    nodes = await repo.knowledge_link_fanout(kind, id, depth=depth, limit=limit)
    return {"success": True, "data": nodes}
