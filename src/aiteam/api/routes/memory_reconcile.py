"""AI Team OS — Memory reconcile routes (memory system v2 P2).

Design §4 "reconcile on demand" — two endpoints:
- GET  /api/memory/reconcile/candidates —— deterministic coarse screening (zero LLM): episodic-layer candidate groups
  (in-cluster BM25 pairing) + direction-layer inventory (for per-item staleness checks) + distillation material
  (promotion candidates) +
  operating instructions. Judgment is left to the in-session CC agent calling the tool (the OS has no independent LLM
  credentials).
- POST /api/memory/reconcile/apply —— batch-apply the operations confirmed by the agent
  (merge/invalidate/score/promote), idempotent: repeating an operation on an already-invalidated entry returns noop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_scoped_repository
from aiteam.api.routes.memory import (
    _MAX_CONTENT_CHARS,
    _bucket_quota_check,
    _resolve_scope_id,
)
from aiteam.api.schemas import ReconcileApply
from aiteam.memory.content_safety import scan_direction_content
from aiteam.memory.reconcile import OPERATION_GUIDE, build_candidate_groups
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/memory/reconcile", tags=["memory"])

# If the number of candidate groups exceeds this count, hint to enable ultracode for concurrent fine judgment (design §4
# trigger clause).
_ULTRACODE_GROUP_HINT = 8


@router.get("/candidates")
async def reconcile_candidates(
    scope_path: str = Query(
        "", description="Only reconcile memos within this path scope (empty = all valid memos in the project)"
    ),
    threshold: float = Query(
        0.45, ge=0.0, le=1.0, description="In-cluster BM25 similarity pairing threshold"
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Coarse screening: return episodic-layer candidate groups + direction-layer inventory + distillation material +
    operating instructions."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail="Memory reconcile requires project context — please call it from within a project (X-Project-Id / "
                   "X-Project-Dir)",
        )

    memos = await repo.list_project_task_memos(
        project_id, scope_path=scope_path or None
    )
    groups = build_candidate_groups(memos, threshold=threshold)
    promotion = [g for g in groups if g["promotion_candidate"]]

    # Direction-layer inventory: the full text of all valid entries, for the caller to judge item by item whether each
    # "is still true" (staleness detection).
    directions = await repo.list_direction_memories(project_id=project_id)
    direction_inventory = [
        {
            "id": m.id,
            "kind": m.kind,
            "scope": m.scope.value,
            "scope_id": m.scope_id,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in directions
    ]

    stats = {
        "project_id": project_id,
        "total_valid_memos": len(memos),
        "candidate_group_count": len(groups),
        "promotion_candidate_count": len(promotion),
        "direction_count": len(direction_inventory),
    }
    if len(groups) > _ULTRACODE_GROUP_HINT:
        stats["ultracode_hint"] = (
            f"{len(groups)} candidate groups — a large volume; consider enabling ultracode and using a Workflow "
            "to fine-judge all groups concurrently and then apply uniformly."
        )

    return {
        "success": True,
        "data": {
            "candidate_groups": groups,
            "promotion_candidates": promotion,
            "direction_inventory": direction_inventory,
            "operation_guide": OPERATION_GUIDE,
            "stats": stats,
        },
    }


@router.post("/apply")
async def reconcile_apply(
    body: ReconcileApply,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Batch-apply reconcile operations (idempotent). Returns per-item results and refreshes last_reconcile_at at the
    end."""
    project_id = repo._project_scope
    results: list[dict] = []

    for op in body.operations:
        kind = (op.op or "").lower().strip()

        if kind in ("keep", "noop", ""):
            results.append({"op": kind or "noop", "status": "noop"})
            continue

        if kind == "invalidate":
            invalidated: list[str] = []
            noop: list[str] = []
            missing: list[str] = []
            for mid in op.memo_ids:
                existing = await repo.get_task_memo(mid)
                if existing is None:
                    missing.append(mid)
                    continue
                if existing.invalid_at is not None:
                    noop.append(mid)  # already invalidated; a repeated operation → noop
                    continue
                await repo.invalidate_task_memo(mid)
                invalidated.append(mid)
            results.append(
                {
                    "op": "invalidate",
                    "status": "applied" if invalidated else "noop",
                    "invalidated": invalidated,
                    "already_invalid": noop,
                    "not_found": missing,
                }
            )
            continue

        if kind == "merge":
            content = (op.content or "").strip()
            if not content:
                results.append(
                    {"op": "merge", "status": "error", "error": "merge requires content"}
                )
                continue
            # Only merge memos that are still valid; if all are invalidated → noop (idempotent)
            valid = []
            for mid in op.memo_ids:
                m = await repo.get_task_memo(mid)
                if m is not None and m.invalid_at is None:
                    valid.append(m)
            if not valid:
                results.append(
                    {
                        "op": "merge",
                        "status": "noop",
                        "reason": "No valid memos to merge (they may already have been reconciled)",
                    }
                )
                continue
            base = valid[0]
            new_memo = await repo.add_task_memo(
                base.task_id,
                content=content,
                author="reconcile",
                memo_type=op.memo_type or "summary",
                scope_path=op.scope_path or base.scope_path,
                project_id=base.project_id,
            )
            for m in valid:
                await repo.invalidate_task_memo(m.id, invalidated_by=new_memo.id)
            results.append(
                {
                    "op": "merge",
                    "status": "applied",
                    "new_memo_id": new_memo.id,
                    "merged": [m.id for m in valid],
                }
            )
            continue

        if kind == "score":
            if not op.memo_id or op.quality_score is None:
                results.append(
                    {
                        "op": "score",
                        "status": "error",
                        "error": "score requires memo_id and quality_score",
                    }
                )
                continue
            if not (1 <= op.quality_score <= 10):
                results.append(
                    {
                        "op": "score",
                        "status": "error",
                        "error": "quality_score must be between 1 and 10",
                    }
                )
                continue
            scored = await repo.score_task_memo(
                op.memo_id, op.quality_score, op.reason
            )
            if scored is None:
                results.append(
                    {"op": "score", "status": "error", "error": f"memo {op.memo_id} not found"}
                )
                continue
            results.append(
                {
                    "op": "score",
                    "status": "applied",
                    "memo_id": op.memo_id,
                    "quality_score": op.quality_score,
                }
            )
            continue

        if kind == "promote":
            content = (op.content or "").strip()
            if not content:
                results.append(
                    {"op": "promote", "status": "error", "error": "promote requires content"}
                )
                continue
            if op.scope not in ("global", "project", "user"):
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": f"promote scope must be one of global/project/user, got {op.scope!r}",
                    }
                )
                continue
            if op.kind not in repo.DIRECTION_KINDS:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": f"kind must be one of {'/'.join(repo.DIRECTION_KINDS)}, got {op.kind!r}",
                    }
                )
                continue
            # Security scan: promote is the direction layer's second write entry point; the same rules as memory_add
            # apply
            finding = scan_direction_content(content)
            if finding is not None:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": finding.message,
                        "safety": {
                            "category": finding.category,
                            "pattern": finding.pattern,
                        },
                    }
                )
                continue
            # Red line 1: a single entry must be ≤ 400 characters
            if len(content) > _MAX_CONTENT_CHARS:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": (
                            f"Content is {len(content)} characters, exceeding the direction layer's per-entry limit of "
                            f"{_MAX_CONTENT_CHARS} — "
                            "please trim it or convert it into a pointer entry"
                        ),
                    }
                )
                continue
            scope_id = _resolve_scope_id(op.scope, "", repo)
            # Red line 2: bucket character quota (storage cap = injection budget, the same axis as memory_add).
            # We do not re-attach the full bucket inventory here — the reconcile flow's direction_inventory already
            # provided the full text,
            # and repeating it on every failed promote would only bloat the response.
            over_quota = await _bucket_quota_check(
                repo, op.scope, scope_id, len(content), include_entries=False
            )
            if over_quota is not None:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": over_quota["error"],
                        "quota": over_quota["quota"],
                    }
                )
                continue
            memory = await repo.create_memory(
                scope=op.scope,
                scope_id=scope_id,
                content=content,
                kind=op.kind,
                source_refs=op.source_refs,
            )
            results.append(
                {"op": "promote", "status": "applied", "memory_id": memory.id}
            )
            continue

        results.append(
            {"op": kind, "status": "error", "error": f"Unknown operation {kind!r}"}
        )

    # Refresh the reconcile timestamp (reuses project.config, no new table) — the baseline for the volume-threshold soft
    # hint.
    reconciled_at = None
    if project_id:
        applied_any = any(r.get("status") == "applied" for r in results)
        if applied_any:
            when = await repo.set_last_reconcile_at(project_id)
            reconciled_at = when.isoformat() if when else None

    return {
        "success": True,
        "data": {
            "results": results,
            "applied_count": sum(1 for r in results if r.get("status") == "applied"),
            "last_reconcile_at": reconciled_at,
        },
    }
