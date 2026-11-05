"""AI Team OS — Task memo tracking routes.

Provides task memo read and append functionality for recording task progress, decisions, issues, and summaries.
Memory system v2 P0: memos have been promoted from the Task.config["memo"] JSON array to a standalone task_memos table;
the write API remains fully compatible, and reads/writes all go through the table (invalidated entries are filtered out by default, invalid_at IS NULL).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import MemoEntry
from aiteam.api.task_edge import try_record_worked_on
from aiteam.memory.content_safety import scan_invisible
from aiteam.storage.repository import StorageRepository, _task_memo_to_legacy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-memo"])

# Memory v2 P2: when the number of new valid memos added to this project since the last reconcile exceeds this count, attach a reconcile hint to the write response.
_RECONCILE_HINT_THRESHOLD = 150


@router.get("/api/tasks/{task_id}/memo")
async def get_task_memo(
    task_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Get task memo record list (queries the task_memos table directly; only valid entries are returned by default)."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    memos = await repo.list_task_memos(task_id)
    return {"success": True, "data": [_task_memo_to_legacy(m) for m in memos]}


@router.post("/api/tasks/{task_id}/memo")
async def add_task_memo(
    task_id: str,
    body: MemoEntry,
    request: Request,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Append a memo record (writes to the task_memos table; when supersedes is given, the old entry is replaced)."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Write-time security scan (v2.1): the episodic layer **only scans for invisible Unicode**. The memo path is high-frequency and does not enter
    # any agent's system prompt — injection-style phrasing here is merely recorded text, not an executed
    # instruction; but content invisible to the naked eye should not be stored in any layer (retrieval would surface it back before the model's eyes).
    finding = scan_invisible(body.content or "")
    if finding is not None:
        return {
            "success": False,
            "error": finding.message,
            "safety": {"category": finding.category, "pattern": finding.pattern},
        }

    memo = await repo.add_task_memo(
        task_id,
        content=body.content,
        author=body.author,
        memo_type=body.type,
        project_id=task.project_id,
        supersedes=body.supersedes,
    )
    entry = _task_memo_to_legacy(memo)

    # Knowledge layer P1a: extract cross-domain references to build edges (zero-LLM regex, best-effort, never blocks the write).
    # Attached at the route layer = the convergence point of the MCP tool and REST dual entry points. from_id uses the real memo id.
    try:
        from aiteam.api.link_extract import extract_refs
        from aiteam.types import KnowledgeLink

        refs = extract_refs(body.content)
        if refs:
            await repo.insert_knowledge_links([
                KnowledgeLink(
                    from_kind="task_memo",
                    from_id=memo.id,
                    to_kind=r.to_kind,
                    to_id=r.to_id,
                    link_type=r.link_type,
                    context=r.context,
                    link_source="regex-memo",
                    project_id=task.project_id or "",
                )
                for r in refs
            ])
    except Exception:  # noqa: BLE001
        logger.warning("memo link extraction failed", exc_info=True)

    # Attribution v1 §2.4: within this bookkeeping action that **already necessarily happens**, also record the agent→task edge.
    # The parameters naturally carry both task_id and author, so nobody needs to make an extra tool call.
    await try_record_worked_on(
        repo,
        task_id=task_id,
        author=body.author,
        request=request,
        project_id=task.project_id or "",
        origin="task_memo_add",
    )

    result: dict = {"success": True, "data": entry}

    # Memory v2 P2 volume-threshold soft hint: new valid memos in this project since the last reconcile > 150 → attach a hint
    # prompting a memory_reconcile pass (a minimal take on Generative Agents' importance-over-threshold: counting by volume).
    if task.project_id:
        try:
            since = await repo.get_last_reconcile_at(task.project_id)
            new_count = await repo.count_valid_task_memos_since(task.project_id, since)
            if new_count > _RECONCILE_HINT_THRESHOLD:
                result["hint"] = (
                    f"This project has gained {new_count} new valid memos since the last reconcile (threshold "
                    f"{_RECONCILE_HINT_THRESHOLD}) — consider calling memory_reconcile_candidates "
                    "to reconcile on demand (for large volumes, enable ultracode concurrency)."
                )
        except Exception:  # noqa: BLE001
            logger.warning("reconcile hint computation failed", exc_info=True)

    return result
