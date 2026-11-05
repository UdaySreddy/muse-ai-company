"""AI Team OS — Wake actionable route.

The single read-only criterion endpoint of wake system v2 (docs/wake-loop-v2-design.md §7.1).
The event watcher (scripts/os-watch.sh) polls it, and the turn-end guard (hooks/turn_end_guard.py)
queries it at shutdown; both share the same criterion. The criterion logic is centralized in wake_actionable.py
(unit-testable); this file is only a thin
HTTP wrapper.

Uses get_repository (without project scope): the criterion module filters explicitly by team_id/session_id/project_id
internally,
avoiding double filtering caused by the ambient _project_scope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api import wake_actionable
from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["wake"])


@router.get("/api/wake/actionable")
async def get_wake_actionable(
    session_id: str = "",
    team_id: str = "",
    project_id: str = "",
    since: str = "",
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Determine whether the Leader session currently has events "worth waking up to handle".

    Query params (all optional):
    - session_id: the startup session the workflow_runs belong to
    - team_id: the team the agents belong to
    - project_id: the project the task_memos/briefings belong to (resolved from the team by default)
    - since: ISO8601 time watermark (with or without offset; read as UTC when no offset), only incremental events after
      it are counted

    See wake_actionable.compute_actionable for the return value. Never returns 500: any internal failure degrades to a
    conservative value.
    """
    return await wake_actionable.compute_actionable(
        repo,
        session_id=session_id,
        team_id=team_id,
        project_id=project_id,
        since_raw=since or None,
    )
