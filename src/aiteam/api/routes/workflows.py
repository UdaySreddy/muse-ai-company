"""AI Team OS — Workflow observability routes (I3a).

Read-only queries for CC ultracode/Workflow run records + per-agent telemetry, plus a manual reconciliation endpoint.

Project isolation uses the optional ``?project_id=`` query (not the ``X-Project-Id`` header): the frontend apiFetch only
attaches the isolation header to ``/api/ecosystem`` (a hard gate in client.ts); non-ecosystem endpoints use
get_repository
with the global repository + explicit query filtering, avoiding a repeat of the all-teams-disappeared bug (red line 2:
do not touch project_scope).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent, WorkflowRun

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowReconcileBody(BaseModel):
    """POST /reconcile request body (all fields optional)."""

    session_id: str | None = None
    project_dir: str | None = None


class WorkflowReconcileResult(BaseModel):
    """Reconciliation result counts."""

    success: bool = True
    ingested: int = 0
    updated: int = 0
    errors: int = 0
    scanned: int = 0


@router.get("", response_model=APIListResponse[WorkflowRun])
async def list_workflow_runs(
    status: str = Query("", description="Filter by status: planned/running/completed/interrupted/killed/failed"),
    project_id: str = Query("", description="Filter by project (empty = all projects)"),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[WorkflowRun]:
    """List materialized workflow runs (naturally excludes the workflow-session-* fallback teams)."""
    runs = await repo.list_workflow_runs(project_id=project_id, status=status, limit=limit)
    return APIListResponse(data=runs, total=len(runs))


@router.get("/{wf_id}", response_model=WorkflowRun)
async def get_workflow_run(
    wf_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> WorkflowRun:
    """Run details (phases, planned-vs-actual, run-level totals, summary/result)."""
    run = await repo.get_workflow_run(wf_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Workflow run '{wf_id}' not found")
    return run


@router.get("/{wf_id}/agents", response_model=APIListResponse[WorkflowAgent])
async def list_workflow_agents(
    wf_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[WorkflowAgent]:
    """Per-agent telemetry (label/model/tokens/toolCalls/duration/state/lastTool/resultPreview)."""
    agents = await repo.list_workflow_agents(wf_id)
    return APIListResponse(data=agents, total=len(agents))


@router.post("/reconcile", response_model=WorkflowReconcileResult)
async def reconcile_workflows(
    body: WorkflowReconcileBody | None = None,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> WorkflowReconcileResult:
    """Manual reconciliation: scan workflows/wf_*.json under the proj-slug and ingest them file by file.

    The SessionStart hook, the Dashboard "Refresh" action, and MCP workflow_reconcile share the same pure function.
    """
    from aiteam.api import workflow_ingest

    body = body or WorkflowReconcileBody()
    result = await workflow_ingest.reconcile(
        repo,
        event_bus,
        project_dir=body.project_dir,
        session_id=body.session_id,
    )

    # Phase 2 optional enhancement: for running/interrupted runs, also perform a live tail + .output enrichment,
    # so a manual refresh shows the live state immediately without waiting for the 60s reaper tick. Best-effort;
    # failures do not affect the reconciliation result.
    live: list[WorkflowRun] = []
    for st in ("running", "interrupted"):
        try:
            live.extend(await repo.list_workflow_runs(status=st, limit=200))
        except Exception:  # noqa: BLE001
            pass
    live.sort(key=lambda r: r.updated_at or r.created_at)
    for run in live[: workflow_ingest.WF_LIVE_TAIL_MAX_RUNS]:
        try:
            res = await workflow_ingest.tail_live_run(repo, event_bus, run)
            newly_marked = bool(isinstance(res, dict) and res.get("marked_interrupted"))
            if run.status == "interrupted" or newly_marked:
                await workflow_ingest.enrich_from_task_output(repo, run)
        except Exception:  # noqa: BLE001 — manual reconciliation must not error out because live enrichment failed
            pass

    return WorkflowReconcileResult(
        success=True,
        ingested=result.get("ingested", 0),
        updated=result.get("updated", 0),
        errors=result.get("errors", 0),
        scanned=result.get("scanned", 0),
    )
