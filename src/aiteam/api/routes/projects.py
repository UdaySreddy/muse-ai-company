"""AI Team OS — Project management + phase management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api import session_probe, worktree_probe
from aiteam.api.deps import get_repository
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    PhaseCreate,
    PhaseStatusUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from aiteam.clock import ensure_utc, utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import Phase, PhaseStatus, Project, TaskStatus, TeamStatus

router = APIRouter(prefix="/api/projects", tags=["projects"])

# ================================================================
# Project CRUD
# ================================================================


@router.post("", response_model=APIResponse[Project], status_code=201)
async def create_project(
    body: ProjectCreate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Project]:
    """Create a project with an automatically created default Phase."""
    project = await repo.create_project(
        name=body.name,
        root_path=body.root_path,
        description=body.description,
        config=body.config,
    )
    # Auto-create default Phase
    await repo.create_phase(
        project_id=project.id,
        name="Phase 1",
        description="Default initial phase",
        order=0,
    )
    return APIResponse(data=project, message="Project created successfully")


@router.get("", response_model=APIListResponse[Project])
async def list_projects(
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Project]:
    """List all projects."""
    projects = await repo.list_projects()
    return APIListResponse(data=projects, total=len(projects))


@router.get("/{project_id}", response_model=APIResponse[dict])
async def get_project(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[dict]:
    """Get project details, including phases list."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    phases = await repo.list_phases(project_id)
    data = project.model_dump()
    data["phases"] = [p.model_dump() for p in phases]
    return APIResponse(data=data, message="")


@router.put("/{project_id}", response_model=APIResponse[Project])
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Project]:
    """Update a project."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    project = await repo.update_project(project_id, **updates)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return APIResponse(data=project, message="Project updated successfully")


@router.delete("/{project_id}", response_model=APIResponse[bool])
async def delete_project(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[bool]:
    """Delete a project."""
    result = await repo.delete_project(project_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return APIResponse(data=True, message="Project deleted successfully")


@router.get("/{project_id}/summary")
async def project_summary(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Quick project summary: status, active teams, top pending tasks."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Get all teams for this project
    teams = await repo.list_teams_by_project(project_id)
    active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]

    # Get pending tasks
    pending_tasks = await repo.list_tasks_by_project(project_id, status=TaskStatus.PENDING)
    running_tasks = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)

    # Live CC session: a leader agent bound to this project whose last_active_at
    # is fresh (hooks refresh it on every tool call) means someone is working in
    # this project right now, even with no running task on the wall.
    # Clock convention: every timestamp in this system is UTC (aiteam/clock.py),
    # and the ORM hands them back timezone-aware — so a freshness window is just
    # arithmetic, with no zone to get wrong.
    from datetime import timedelta

    live_session = False
    last_activity_at: str | None = None
    leader_info: dict | None = None
    leaders_info: list[dict] = []

    # Leader identity = the CC main session under this project directory (read directly from the file source of truth,
    # zero registration dependency).
    # User ruling (2026-07-07): model/active status is auto-detected by the backend, not via the hook registration chain
    # —
    # the registration chain broke twice before (the leader row parasitized a workflow team and was migrated across
    # projects; compact synthesized rows polluted it).
    # User ruling (2026-07-10): when multiple sessions run in parallel, show each one as CEO-<English name>, no longer
    # just the latest one.
    for probe in session_probe.detect_live_sessions(
        getattr(project, "root_path", "") or ""
    ):
        # In-flight tasks (fleet layer P2 observation, see docs/fleet-layer-design.md §6.1):
        # the number of running tasks in the teams of the agents under this session (leader + the sub-agents it
        # dispatched).
        # Best-effort caliber when there is no owner_session_id (that is the fleet P1 foundation, not done in this
        # batch) —
        # if probing fails or the session has no agents persisted yet, honestly record 0; do not guess and do not abort
        # with an error.
        in_flight_tasks = 0
        try:
            session_agents = await repo.find_agents_by_session(probe["session_id"])
            team_ids = {a.team_id for a in session_agents if a.team_id}
            for tid in team_ids:
                running = await repo.list_tasks(tid, status=TaskStatus.RUNNING)
                in_flight_tasks += len(running)
        except Exception:  # noqa: BLE001 — summary must not fail on this metric
            in_flight_tasks = 0

        leaders_info.append({
            "name": f"CEO-{probe['name']}",
            "model": probe["model"],
            "status": "busy" if probe["live"] else "offline",
            "session_id": probe["session_id"],
            "current_task": "",
            "last_active_at": probe["last_active_at"],
            "live": bool(probe["live"]),
            # CC session registry parallel reading (C13 dual-track observation) — display only, does not participate in
            # the
            # status/live determination above. cc_status is CC's own idle/busy.
            "cc_pid": probe.get("cc_pid"),
            "cc_status": probe.get("cc_status", ""),
            "cc_process_alive": probe.get("cc_process_alive"),
            "ctx_tokens": probe.get("ctx_tokens"),
            "ctx_window": probe.get("ctx_window"),
            "ctx_pct": probe.get("ctx_pct"),
            "in_flight_tasks": in_flight_tasks,
        })
    if leaders_info:
        live_session = any(li["live"] for li in leaders_info)
        last_activity_at = leaders_info[0]["last_active_at"]
        leader_info = leaders_info[0]

    # The DB leader row only supplements (fields only the hook chain has, such as current_task) and serves as a fallback
    # when probing is unavailable.
    try:
        leaders = await repo.find_agents_by_role("leader")
        now = utc_now()
        freshest = None
        freshest_leader = None
        for leader in leaders:
            if getattr(leader, "project_id", None) != project_id:
                continue
            ts = ensure_utc(getattr(leader, "last_active_at", None))
            if ts is None:
                continue
            if freshest is None or ts > freshest:
                freshest = ts
                freshest_leader = leader
        if freshest_leader is not None:
            if leader_info is None:
                # Aware UTC — isoformat() carries "+00:00", so the browser reads
                # the instant rather than guessing a zone.
                last_activity_at = freshest.isoformat() if freshest else None
                live_session = bool(
                    freshest and (now - freshest) < timedelta(minutes=15)
                )
                db_in_flight = 0
                try:
                    if getattr(freshest_leader, "team_id", None):
                        running = await repo.list_tasks(
                            freshest_leader.team_id, status=TaskStatus.RUNNING
                        )
                        db_in_flight = len(running)
                except Exception:  # noqa: BLE001 — summary must not fail on this metric
                    db_in_flight = 0
                leader_info = {
                    "name": freshest_leader.name,
                    "model": getattr(freshest_leader, "model", "") or "",
                    "status": str(getattr(freshest_leader, "status", "")),
                    "session_id": getattr(freshest_leader, "session_id", "") or "",
                    "current_task": getattr(freshest_leader, "current_task", "")
                    or "",
                    "last_active_at": last_activity_at,
                    "live": live_session,
                    # When there is no file-probe data (DB fallback path), the watermark is unknown; honestly leave it
                    # empty —
                    # never misapply the agents table's sub-agent watermark caliber to the main session row.
                    "ctx_tokens": None,
                    "ctx_window": None,
                    "ctx_pct": None,
                    "in_flight_tasks": db_in_flight,
                }
                leaders_info.append(leader_info)
            else:
                # current_task only exists in the hook chain — supply it to the corresponding session entry by
                # session_id
                db_sid = getattr(freshest_leader, "session_id", "")
                for li in leaders_info:
                    if li["session_id"] == db_sid:
                        li["current_task"] = (
                            getattr(freshest_leader, "current_task", "") or ""
                        )
    except Exception:  # noqa: BLE001 — summary must not fail on liveness probe
        pass

    # Determine project status: active only if work is actively in progress
    # (any team active, any task running, or a live CC session in this project).
    # Pending backlog alone doesn't count — every project with unfinished tasks
    # would otherwise be "active" forever.
    is_active = len(active_teams) > 0 or len(running_tasks) > 0 or live_session

    # Top 3 pending tasks sorted by priority
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    top_tasks = sorted(
        pending_tasks,
        key=lambda t: priority_order.get(str(t.priority), 99),
    )[:3]

    # Number of distinct CC sessions that have appeared under this project (agents.session_id footprint)
    try:
        session_count = await repo.count_project_sessions(project_id)
    except Exception:  # noqa: BLE001 — summary must not fail on this metric
        session_count = 0

    # Worktree observation (docs/worktree-governance-design.md §4/(c)): scanned on demand,
    # no background daemon — each summary request triggers one read-only git probe, following the same principle as the
    # other probe sections in this function
    # (probe failures degrade silently and never make the whole summary error out).
    try:
        worktrees = worktree_probe.detect_worktrees(getattr(project, "root_path", "") or "")
    except Exception:  # noqa: BLE001 — summary must not fail on this metric
        worktrees = []

    return {
        "status": "active" if is_active else "inactive",
        "active_teams": len(active_teams),
        "pending_tasks": len(pending_tasks),
        "running_tasks": len(running_tasks),
        "session_count": session_count,
        "last_activity_at": last_activity_at,
        "leader": leader_info,
        "leaders": leaders_info,
        "worktrees": worktrees,
        "top_tasks": [
            {"title": t.title, "priority": str(t.priority)}
            for t in top_tasks
        ],
    }


# ================================================================
# Phase management
# ================================================================


@router.post(
    "/{project_id}/phases",
    response_model=APIResponse[Phase],
    status_code=201,
)
async def create_phase(
    project_id: str,
    body: PhaseCreate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Phase]:
    """Create a phase."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    phase = await repo.create_phase(
        project_id=project_id,
        name=body.name,
        description=body.description,
        order=body.order,
        config=body.config,
    )
    return APIResponse(data=phase, message="Phase created successfully")


@router.get("/{project_id}/phases", response_model=APIListResponse[Phase])
async def list_phases(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Phase]:
    """List all phases under a project."""
    phases = await repo.list_phases(project_id)
    return APIListResponse(data=phases, total=len(phases))


# Valid status transitions
_VALID_TRANSITIONS: dict[PhaseStatus, set[PhaseStatus]] = {
    PhaseStatus.PLANNING: {PhaseStatus.ACTIVE, PhaseStatus.ARCHIVED},
    PhaseStatus.ACTIVE: {PhaseStatus.COMPLETED, PhaseStatus.ARCHIVED},
    PhaseStatus.COMPLETED: {PhaseStatus.ARCHIVED, PhaseStatus.ACTIVE},
    PhaseStatus.ARCHIVED: set(),
}


@router.put(
    "/{project_id}/phases/{phase_id}/status",
    response_model=APIResponse[Phase],
)
async def update_phase_status(
    project_id: str,
    phase_id: str,
    body: PhaseStatusUpdate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Phase]:
    """Update phase status with transition validation.

    Constraint: only one Phase can be active at a time within a project.
    """
    # Validate target status
    try:
        target_status = PhaseStatus(body.status)
    except ValueError:
        valid = [s.value for s in PhaseStatus]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{body.status}', options: {valid}",
        )

    # Get current phase
    phase = await repo.get_phase(phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")

    # Verify phase belongs to this project
    if phase.project_id != project_id:
        raise HTTPException(
            status_code=400,
            detail=f"Phase {phase_id} does not belong to project {project_id}",
        )

    # Check status transition validity
    current_status = phase.status
    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Transition from {current_status.value} to {target_status.value} is not allowed,"
            f"allowed: {[s.value for s in allowed]}",
        )

    # If target is active, first set other active phases in the project to completed
    if target_status == PhaseStatus.ACTIVE:
        deactivated = await repo.deactivate_phases(project_id)
        if deactivated > 0:
            msg = f"Set {deactivated} old active phases to completed"
        else:
            msg = ""
    else:
        msg = ""

    updated = await repo.update_phase(phase_id, status=target_status)
    if updated is None:
        raise HTTPException(status_code=500, detail="Update failed")

    message = f"Phase status updated to {target_status.value}"
    if msg:
        message += f" ({msg})"
    return APIResponse(data=updated, message=message)
