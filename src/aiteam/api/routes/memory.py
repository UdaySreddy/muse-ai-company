"""AI Team OS — Memory query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import (
    APIListResponse,
    MemoryCreate,
    MemoryInvalidate,
    MemoryInvalidateByMatch,
)
from aiteam.memory.content_safety import scan_direction_content
from aiteam.memory.scoping import dir_bucket_scope_id
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory

router = APIRouter(prefix="/api/memory", tags=["memory"])

# ================================================================
# Direction-layer memory (memory system v2 P1) — three entry points: POST/GET/invalidate
# ================================================================

# Volume red line — a single axis (v2.1, ruling 2026-07-31): **storage cap = injection budget**.
#
# The old caliber had two different axes: the storage side's "≤40 entries per bucket × ≤400 characters per entry"
# allowed 16,000 characters,
# while the injection-side budget was 900 characters. The consequence of the 17x gap is not "storing more is useless"
# but "stored equals not stored" — measured in practice:
# of 48 valid entries, only the first 2-3 actually made it into the agent's system prompt; the rest were truncated into
# a single line
# "46 more entries", and the direction layer's selling point that "dispatched agents inherit it at birth" was hollowed
# out by its own red line.
#
# Now only one axis remains: the injection pool = the global/system + current project + user buckets jointly capped at
# 3000 characters,
# with per-bucket quotas as below. Capacity pressure is moved forward to the moment of writing (reject when over the
# limit + hand back the full bucket inventory requiring the caller to first
# reconcile), and the injection side no longer truncates routinely — **if it fits in storage, it is guaranteed to be
# delivered**.
# The 400-character per-entry cap is kept: the pointer-entry philosophy (trigger condition + pointing to the
# authoritative file, body kept externally) is unchanged.
_MAX_CONTENT_CHARS = 400  # Per-entry content character cap
_BUCKET_QUOTA_CHARS = {"global": 1200, "project": 1500, "user": 300}
# The injection pool a session actually inherits = one share from each of the three buckets (project buckets are
# quota-counted per project).
_DIRECTION_TOTAL_BUDGET = sum(_BUCKET_QUOTA_CHARS.values())  # 3000

# scope → default scope_id (derived when not given explicitly)
_DEFAULT_SCOPE_ID = {"global": "system", "user": "user"}

router_memories = APIRouter(prefix="/api/memories", tags=["memory"])


def _resolve_scope_id(scope: str, scope_id: str, repo: StorageRepository) -> str:
    """Derive scope_id from scope: project → current project / unregistered-directory temp bucket, global → system, user
    → user.

    Under an unregistered directory (the repo has no _project_scope), scope=project **no longer silently falls back to
    "system"**
    — that would broadcast this directory's project memory as global memory (the root cause of the 2026-07-21
    cross-wiring incident). Instead it uses a
    directory-fingerprint temp bucket "dir:<sha1>" derived from the cwd (the X-Project-Dir header); if even the cwd is
    unavailable (a bare call without the
    header), it raises a 422 rejection advising to include X-Project-Dir or use scope=global instead, and never lands in
    system.
    """
    if scope_id:
        return scope_id
    if scope == "project":
        if repo._project_scope:
            return repo._project_scope
        bucket = dir_bucket_scope_id(repo._unresolved_dir)
        if bucket:
            return bucket
        raise HTTPException(
            status_code=422,
            detail=(
                "Writing with scope=project requires directory context: the current request neither matched a "
                "registered project"
                "nor has a cwd (no X-Project-Dir header). Please write from a session that carries X-Project-Dir,"
                "or use scope=global instead (only when this entry holds for any session in any directory)."
            ),
        )
    return _DEFAULT_SCOPE_ID.get(scope, "system")


async def _bucket_quota_check(
    repo: StorageRepository,
    scope: str,
    scope_id: str,
    incoming_chars: int,
    replaced: Memory | None = None,
    *,
    include_entries: bool = True,
) -> dict | None:
    """Validate against the bucket character quota; return None if it fits, otherwise return the **over-limit protocol**
    response body.

    The caliber is "the bucket's total characters after applying the replacement": supersedes frees the old entry's
    characters, so swapping a
    long entry for a short one should never be rejected (mindlessly adding the new entry's character count would wrongly
    reject it — leftover thinking from the old count axis).

    Over-limit protocol (copied from the Hermes mechanism): it is not settled by replying "reconcile first, then add" —
    instead, hand back the bucket's **current
    full set of valid entries** (id / kind / character count / full text) together with the usage and the shortfall, and
    explicitly require
    freeing space within this round before retrying this write. Reconciliation must happen at the very moment of
    rejection; otherwise the caller
    will just retry with a shorter sentence, and the capacity pressure would still go unhandled.
    """
    quota = _BUCKET_QUOTA_CHARS.get(scope)
    if quota is None:  # non-direction-layer scopes are not quota-constrained (legacy team/agent partitions)
        return None

    entries = await repo.list_memories(scope, scope_id)
    entries.sort(key=lambda m: m.created_at)
    used = sum(len(m.content) for m in entries)
    freed = len(replaced.content) if replaced is not None else 0
    projected = used - freed + incoming_chars
    if projected <= quota:
        return None

    over_by = projected - quota
    replace_note = f"(this replacement can free {freed} characters)" if freed else ""
    where_entries = (
        "The bucket_entries field of this response holds all of the bucket's currently valid entries (including id / "
        "kind / character count / full text):"
        if include_entries
        else "For all of the bucket's valid entries, see the direction_inventory returned by this reconcile (or call "
             "memory_list):"
    )
    payload: dict = {
        "success": False,
        "error": (
            f"Write rejected: the direction-layer quota for scope {scope}/{scope_id} is {quota} characters,"
            f"with {used} characters currently used ({len(entries)} entries), this addition is {incoming_chars} "
            f"characters"
            f"{replace_note}; once stored it would reach {projected} characters, exceeding the limit by {over_by} "
            f"characters.\n"
            "The direction layer's storage cap is exactly the injection budget — only what fits in storage gets "
            "delivered, so nothing can be squeezed in here.\n"
            f"{where_entries}Within **this round**, judge item by item which entries are stale or can be merged, and "
            f"use "
            "memory_invalidate (you may pass a content_match substring to locate them) or "
            f"memory_reconcile_apply to free at least {over_by} characters, then retry this write."
        ),
        "quota": {
            "scope": scope,
            "scope_id": scope_id,
            "quota_chars": quota,
            "used_chars": used,
            "entry_count": len(entries),
            "incoming_chars": incoming_chars,
            "freed_by_supersedes": freed,
            "projected_chars": projected,
            "over_by_chars": over_by,
            "total_injection_budget": _DIRECTION_TOTAL_BUDGET,
        },
        "next_action": (
            f"First invalidate/merge at least {over_by} characters of stale entries in this bucket, then retry this "
            f"write."
        ),
    }
    if include_entries:
        payload["bucket_entries"] = [
            {
                "id": m.id,
                "kind": m.kind,
                "chars": len(m.content),
                "created_at": m.created_at.isoformat(),
                "content": m.content,
            }
            for m in entries
        ]
    return payload


@router_memories.post("")
async def create_direction_memory(
    body: MemoryCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Write one direction-layer memory (the volume red line is enforced here; over-limit writes are rejected with a
    prompt to reconcile first)."""
    if body.scope not in ("global", "project", "user"):
        raise HTTPException(
            status_code=422,
            detail=f"Direction-layer scope must be one of global/project/user, got {body.scope!r}",
        )
    if body.kind not in repo.DIRECTION_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"kind must be one of {'/'.join(repo.DIRECTION_KINDS)}, got {body.kind!r}"
            ),
        )

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must not be empty")

    # Write-time security scan (v2.1): direction-layer entries enter the system prompt of every dispatched agent,
    # so memory_add is an injection amplifier — invisible characters / prompt-injection phrasing / credential shapes are
    # intercepted here.
    finding = scan_direction_content(content)
    if finding is not None:
        return {
            "success": False,
            "error": finding.message,
            "safety": {"category": finding.category, "pattern": finding.pattern},
        }

    # Volume red line 1: a single entry > 400 characters → reject (over-limit content should be demoted to a "pointer
    # entry" with the body kept externally)
    if len(content) > _MAX_CONTENT_CHARS:
        return {
            "success": False,
            "error": (
                f"Content is {len(content)} characters, exceeding the direction layer's per-entry limit of "
                f"{_MAX_CONTENT_CHARS} characters."
                "The direction layer's value lies in being small and precise: please trim it, or convert it into a "
                "pointer entry of 'trigger condition + pointer to the authoritative file', "
                "and place large bodies in the episodic layer / reports, to be pulled by retrieval on demand."
            ),
        }

    scope_id = _resolve_scope_id(body.scope, body.scope_id, repo)

    # supersedes must actually replace something to count as a replacement (review major: a nonexistent /
    # already-invalidated / cross-bucket
    # supersedes id could once bypass the red line without invalidating any old entry → unbounded net growth). After
    # validation passes,
    # the old entry's character count is counted as "freed" in the quota caliber.
    replaced: Memory | None = None
    if body.supersedes is not None:
        old = await repo.get_memory(body.supersedes)
        if old is None or old.invalid_at is not None:
            return {
                "success": False,
                "error": (
                    f"The memory {body.supersedes} pointed to by supersedes does not exist or has been invalidated,"
                    "so it cannot be written as a replacement. If this is a new entry, remove the supersedes parameter."
                ),
            }
        if old.scope.value != body.scope or old.scope_id != scope_id:
            return {
                "success": False,
                "error": (
                    f"The supersedes target belongs to {old.scope.value}/{old.scope_id},"
                    f"a different bucket from this entry's {body.scope}/{scope_id}; cross-bucket replacement is "
                    f"forbidden."
                ),
            }
        replaced = old

    # Volume red line 2: bucket character quota (storage cap = injection budget) → over-limit cases go through the
    # Hermes over-limit protocol
    over_quota = await _bucket_quota_check(
        repo, body.scope, scope_id, len(content), replaced
    )
    if over_quota is not None:
        return over_quota

    memory = await repo.create_memory(
        scope=body.scope,
        scope_id=scope_id,
        content=content,
        kind=body.kind,
        source_refs=body.source_refs,
        supersedes=body.supersedes,
    )
    return {"success": True, "data": memory.model_dump(mode="json")}


@router_memories.post("/invalidate")
async def invalidate_direction_memory_by_match(
    body: MemoryInvalidateByMatch,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Locate a direction-layer memory by content substring and invalidate it (the Hermes replace/remove locating
    protocol).

    Reconciliation often happens at the very moment of "being bounced back by the over-limit protocol", when what the
    caller has in hand is the entry's original text;
    looking up the id first and then invalidating would be a needless extra hop. The substring must match **uniquely**:
    on 0 or multiple matches, touch no
    data; on multiple matches, hand the candidates back and require a more precise substring — never guess.

    The search surface shares its source with memory_list / injection: global + user + the current project's project
    bucket.
    """
    needle = (body.content_match or "").strip()
    if not needle:
        raise HTTPException(status_code=422, detail="content_match must not be empty")

    project_id = repo._project_scope or dir_bucket_scope_id(repo._unresolved_dir) or None
    candidates = await repo.list_direction_memories(project_id=project_id)
    matched = [m for m in candidates if needle in m.content]

    if not matched:
        return {
            "success": False,
            "error": (
                f"No valid direction-layer entry containing the substring '{needle}' was matched."
                "First check the original text with memory_list (substrings are case- and punctuation-sensitive)."
            ),
        }
    if len(matched) > 1:
        return {
            "success": False,
            "error": (
                f"The substring '{needle}' matched {len(matched)} entries; it is impossible to determine which one to "
                f"invalidate."
                "Please provide a longer unique substring, or pass memory_id directly."
            ),
            "matches": [
                {"id": m.id, "kind": m.kind, "excerpt": m.content[:80]} for m in matched
            ],
        }

    memory = await repo.invalidate_memory(
        matched[0].id, invalidated_by=body.invalidated_by
    )
    return {"success": True, "data": memory.model_dump(mode="json") if memory else None}


@router_memories.post("/{memory_id}/invalidate")
async def invalidate_direction_memory(
    memory_id: str,
    body: MemoryInvalidate | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Explicitly invalidate one direction-layer memory (no deletion; Zep invalidation semantics)."""
    invalidated_by = body.invalidated_by if body else None
    memory = await repo.invalidate_memory(memory_id, invalidated_by=invalidated_by)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return {"success": True, "data": memory.model_dump(mode="json")}


@router_memories.get("", response_model=APIListResponse[Memory])
async def list_direction_memories(
    kind: str = Query("", description="Filter by kind: constraint/design/directive/preference"),
    include_invalidated: bool = Query(False, description="Whether to include invalidated entries"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Memory]:
    """List valid direction-layer entries (valid-only by default), ordered by kind priority + time descending.

    Automatically includes global + user global entries, as well as the current project's (X-Project-Id / X-Project-Dir)
    project-level entries — the data source for the dual hooks' standing injection. For an unregistered directory (no
    _project_scope,
    but carrying X-Project-Dir), reads return this directory's fingerprint temp bucket "dir:<sha1>", symmetric with the
    write path:
    you store your own and inherit your own, and cannot read other directories' temp buckets (root fix for the
    2026-07-21 cross-wiring incident).
    """
    project_id = repo._project_scope or dir_bucket_scope_id(repo._unresolved_dir) or None
    memories = await repo.list_direction_memories(
        project_id=project_id,
        kind=kind or None,
        include_invalidated=include_invalidated,
    )
    return APIListResponse(data=memories, total=len(memories))


@router.get("", response_model=APIListResponse[Memory])
async def search_memories(
    scope: str = Query("global", description="Memory scope"),
    scope_id: str = Query(
        "",
        description=(
            "Scope ID; leave empty to derive it from scope (global→system, user→user,"
            "project→current project id or the unregistered-directory fingerprint temp bucket)"
        ),
    ),
    query: str = Query("", description="Search keywords"),
    limit: int = Query(10, ge=1, le=100, description="Return count limit"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Memory]:
    """Search memories (when scope_id is omitted, it is derived from context under the same rules as the list/write
    paths).

    2026-07-21 incident follow-up: this endpoint originally used a non-scoped repo + an explicit scope_id (default
    system),
    completely bypassing the X-Project-Dir chain — under an unregistered directory, memory_search could not find the
    temp bucket it had just written.
    It now depends on get_scoped_repository: under the project scope, when scope_id is omitted (including the old MCP
    default placeholder
    "system"), the scoped repo resolves the current project id or the unregistered directory's fingerprint bucket; if
    there is not even a cwd,
    it returns 422. **An explicitly passed scope_id is always respected** (explicit-parameter paths such as the M4
    patrol are unaffected).
    """
    effective_scope_id = scope_id
    if scope == "project" and effective_scope_id == "system":
        # Under project, "system" is the old memory_search MCP default placeholder and is meaningless → treated as
        # unspecified
        effective_scope_id = ""
    resolved_scope_id = _resolve_scope_id(scope, effective_scope_id, repo)
    if query:
        memories = await repo.search_memories(scope, resolved_scope_id, query, limit)
    else:
        memories = await repo.list_memories(scope, resolved_scope_id)
        memories = memories[:limit]
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Team knowledge base endpoint
# ================================================================

router_teams_memory = APIRouter(prefix="/api/teams", tags=["memory"])


@router_teams_memory.get("/{team_id}/knowledge", response_model=APIListResponse[Memory])
async def get_team_knowledge(
    team_id: str,
    type: str = Query(
        "", description="Type filter: failure_alchemy / lesson_learned / loop_review"
    ),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get team knowledge base.

    Returns the team's scope=team memory list, including:
    - failure_alchemy generated failure lessons
    - lesson_learned manually recorded experiences
    - loop_review retrospective summaries
    Sorted by created_at descending, supports ?type= filtering.
    """
    memories = await repo.list_team_knowledge(
        team_id=team_id,
        memory_type=type or None,
        limit=limit,
    )
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Agent experience summary endpoint
# ================================================================

router_agents_memory = APIRouter(prefix="/api/agents", tags=["memory"])


@router_agents_memory.get("/{agent_id}/experience", response_model=APIListResponse[Memory])
async def get_agent_experience(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get Agent experience summary.

    Returns the Agent's scope=agent memory list,
    including task completion records and accumulated experience.
    """
    memories = await repo.list_agent_experience(agent_id=agent_id, limit=limit)
    return APIListResponse(data=memories, total=len(memories))
