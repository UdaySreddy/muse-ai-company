"""Compaction checkpoint — before the context is compacted, take a snapshot of the OS-side operational state.

When CC compacts a session, the model's own memory is taken over by CC's compact_summary; **what gets lost is the OS
side's operational state**: which agents this session dispatched, what they are doing, what is still open on the project
wall,
and which pending decisions are hanging with the Creator. All of these live in the database the whole time; the
post-compaction Leader simply does not
know to ask for them. The checkpoint freezes them at the moment of compaction and hands them back as-is at the first
post-compaction SessionStart
(source=compact).

It deliberately does not store CC's compact_summary: that summary **is already in the model's context after
compaction**, so injecting it
once more would be duplication; what the OS should supply is the half the model does not have.

Storage uses the events table (``session.compact_checkpoint``, located by ``session:<id>``):
Compaction is a low-frequency event (159 times in 23 days on this machine) and is not worth a dedicated table and a
migration; the event stream
naturally carries timestamps and is naturally append-only, which fits checkpoint semantics exactly.
"""

from __future__ import annotations

import logging
from typing import Any

from aiteam.api import background_jobs
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

logger = logging.getLogger(__name__)

CHECKPOINT_EVENT = "session.compact_checkpoint"

_MAX_AGENTS = 12
_MAX_TASKS = 10
_MAX_BRIEFINGS = 5


async def build_snapshot(
    repo: StorageRepository, session_id: str, cwd: str = ""
) -> dict[str, Any]:
    """Collect this session's current OS-side operational state into a JSON-serializable snapshot.

    Every block is individually guarded: if a block cannot be queried, leave it empty; never let one probe failure ruin
    the entire checkpoint
    — at the moment of compaction there is no second chance.
    """
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "leader": None,
        "agents": [],
        "open_tasks": [],
        "pending_briefings": [],
        "background_jobs": [],
    }

    # CC's --bg background sessions are not registered through hooks, and the post-compaction Leader is especially prone
    # to forgetting they are still running.
    try:
        snapshot["background_jobs"] = [
            {"job_id": j.job_id, "intent": j.intent, "cwd": j.cwd, "state": j.state}
            for j in background_jobs.in_flight_jobs(cwd)
        ]
    except Exception:  # noqa: BLE001
        logger.debug("compact checkpoint: background job probe failed", exc_info=True)

    project_id = ""
    try:
        agents = await repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.role == "leader":
                snapshot["leader"] = {"name": agent.name, "model": agent.model or ""}
                project_id = agent.project_id or ""
                continue
            if agent.status.value == "offline":
                continue
            snapshot["agents"].append(
                {
                    "name": agent.name,
                    "status": agent.status.value,
                    "current_task": agent.current_task or "",
                    "ctx_pct": agent.ctx_pct,
                }
            )
        snapshot["agents"] = snapshot["agents"][:_MAX_AGENTS]
    except Exception:  # noqa: BLE001
        logger.debug("compact checkpoint: agent snapshot failed", exc_info=True)

    if project_id:
        snapshot["project_id"] = project_id
        try:
            running = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)
            pending = await repo.list_tasks_by_project(project_id, status=TaskStatus.PENDING)
            for task in (running + pending)[:_MAX_TASKS]:
                snapshot["open_tasks"].append(
                    {
                        "id": task.id,
                        "title": task.title,
                        "status": task.status.value,
                        "assigned_to": task.assigned_to or "",
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("compact checkpoint: task snapshot failed", exc_info=True)

        try:
            briefings = await repo.list_briefings(status="pending", project_id=project_id)
            for item in briefings[:_MAX_BRIEFINGS]:
                snapshot["pending_briefings"].append(
                    {"id": item.id, "title": item.title, "urgency": item.urgency}
                )
        except Exception:  # noqa: BLE001
            logger.debug("compact checkpoint: briefing snapshot failed", exc_info=True)

    return snapshot


def render(snapshot: dict[str, Any]) -> str:
    """Render the snapshot into a text block that is injected directly into the Leader's context after compaction.

    An empty snapshot returns an empty string — when there is nothing to say, do not occupy the user's context.
    """
    agents = snapshot.get("agents") or []
    tasks = snapshot.get("open_tasks") or []
    briefings = snapshot.get("pending_briefings") or []
    jobs = snapshot.get("background_jobs") or []
    if not (agents or tasks or briefings or jobs):
        return ""

    lines = ["", "## Pre-compaction operational state (OS checkpoint)", ""]
    if agents:
        lines.append(f"In-flight Agents ({len(agents)}):")
        for a in agents:
            task = f" — {a['current_task']}" if a.get("current_task") else ""
            ctx = f" (context {a['ctx_pct']:.0%})" if a.get("ctx_pct") else ""
            lines.append(f"  - {a['name']} [{a['status']}]{task}{ctx}")
        lines.append("")
    if tasks:
        lines.append(f"Unfinished tasks ({len(tasks)}):")
        for t in tasks:
            owner = f" @{t['assigned_to']}" if t.get("assigned_to") else ""
            lines.append(f"  - [{t['status']}] {t['title']}{owner}")
        lines.append("")
    if jobs:
        lines.append(f"In-flight background jobs ({len(jobs)}):")
        for j in jobs:
            intent = f" — {j['intent']}" if j.get("intent") else ""
            lines.append(f"  - {j['job_id']} [{j['state']}]{intent}")
        lines.append("")
    if briefings:
        lines.append(f"Awaiting the Creator's decision ({len(briefings)}):")
        for b in briefings:
            lines.append(f"  - [{b['urgency']}] {b['title']} (id {b['id'][:8]})")
        lines.append("")
    lines.append(
        "The above is the actual state in the OS database at the moment of compaction, not a recollection. For "
        "details, query live with task_memo_read / "
        "briefing_list / taskwall_view; do not infer from this summary."
    )
    return "\n".join(lines)
