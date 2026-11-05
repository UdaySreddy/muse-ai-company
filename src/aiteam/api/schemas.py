"""AI Team OS — API request/response schemas.

Defines unified response wrappers and request models.
Response data fields reuse Pydantic models from types.py.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ============================================================
# Unified response wrappers
# ============================================================


class APIResponse(BaseModel, Generic[T]):
    """Unified API response."""

    success: bool = True
    data: T | None = None
    message: str = ""


class APIListResponse(BaseModel, Generic[T]):
    """Unified list response."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    total: int = 0
    message: str = ""


# ============================================================
# Request models
# ============================================================


class TeamUpdate(BaseModel):
    """Update team request."""

    mode: str | None = None
    status: str | None = None
    # Project ownership reassignment (added 2026-07-26): previously no endpoint could change team.project_id — once
    # automatic
    # attribution was inferred wrongly (the hook fallback guessed the wrong project), it was stuck forever, with the
    # team displayed in the wrong project's view.
    # Passing an empty string means unbinding (back to unowned, awaiting rebinding via the next authoritative path).
    project_id: str | None = None


class AgentCreate(BaseModel):
    """Create Agent request."""

    name: str
    role: str
    system_prompt: str = ""
    # Left empty by default: when the model is unknown, no concrete model is recorded (it is backfilled by transcript
    # observation),
    # a concrete version hardcoded into a default inevitably goes stale (the 4.7 leftover is of this kind) — rule
    # established 2026-07-07
    model: str = ""


class TaskCreate(BaseModel):
    """Create task request."""

    title: str
    description: str = ""


class TaskRun(BaseModel):
    """Run task request."""

    description: str
    title: str = ""
    model: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)
    assigned_to: str | None = None


class MemoryQuery(BaseModel):
    """Memory query request."""

    scope: str = "global"
    scope_id: str = "system"
    query: str = ""
    limit: int = 10


class AgentStatusUpdate(BaseModel):
    """Update Agent status request."""

    status: str
    current_task: str | None = None


class ProjectCreate(BaseModel):
    """Create project request."""

    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    root_path: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class PhaseCreate(BaseModel):
    """Create phase request."""

    name: str
    description: str = ""
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class PhaseStatusUpdate(BaseModel):
    """Update phase status request."""

    status: str


class MeetingCreate(BaseModel):
    """Create meeting request."""

    topic: str
    participants: list[str] = Field(default_factory=list)
    meta_json: dict = Field(default_factory=dict)


class SubtaskInput(BaseModel):
    """Subtask input."""

    title: str
    description: str = ""


class TaskDecompose(BaseModel):
    """Task decomposition request."""

    title: str
    description: str = ""
    template: str = ""  # web-app/api-service/data-pipeline/library/refactor/bugfix
    subtasks: list[SubtaskInput] | None = None
    auto_assign: bool = False
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)


class TaskCreateBody(BaseModel):
    """Project-level task creation request."""

    title: str
    description: str = ""
    priority: str = "medium"
    horizon: str = "mid"
    tags: list[str] = Field(default_factory=list)
    assigned_to: str | None = None
    status: str = "pending"
    # CC bridge field. cc_task_bridge has always been POSTing assigned_to, but this field did not exist here before,
    # so Pydantic silently dropped it — mirrored tasks never had an owner.
    cc_task_id: str | None = None
    # CC's blockedBy (CC's own task ids). The server resolves those that have been mirrored into the OS's
    # depends_on; unresolvable ones stay as-is in config as evidence — never stuff a CC id directly into
    # depends_on masquerading as an OS task id.
    cc_blocked_by: list[str] = Field(default_factory=list)


class TaskUpdateBody(BaseModel):
    """Partial update task request — all fields optional."""

    status: str | None = None
    assigned_to: str | None = None
    result: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    title: str | None = None
    description: str | None = None


class IssueReport(BaseModel):
    """Report issue request."""

    title: str
    description: str = ""
    severity: str = "medium"
    category: str = "bug"


class MemoEntry(BaseModel):
    """Task memo entry request."""

    author: str = "leader"
    content: str
    type: str = "progress"  # progress / decision / issue / summary
    supersedes: str | None = None  # Memory v2: the old memo id superseded by this entry (it gets invalidated)


class MemoryCreate(BaseModel):
    """Direction-layer memory write request (memory system v2 P1).

    scope_id may be left empty and is derived by the service layer from scope: global→"system", user→"user",
    project→current project id (X-Project-Id / X-Project-Dir).
    """

    content: str
    kind: str = "preference"  # constraint / design / directive / preference
    scope: str = "global"  # global / project / user
    scope_id: str = ""
    source_refs: list[str] = Field(default_factory=list)  # Provenance: memo/report/meeting id
    supersedes: str | None = None  # The old memory id invalidated by this entry's replacement


class MemoryInvalidate(BaseModel):
    """Direction-layer memory explicit invalidation request."""

    invalidated_by: str | None = None  # Replacer memory id (optional)


class MemoryInvalidateByMatch(BaseModel):
    """Locate a direction-layer memory by content substring and invalidate it (v2.1 locating protocol).

    The substring must uniquely match a valid entry in the current context (global + user + current project);
    on 0 or multiple matches, no data is touched at all.
    """

    content_match: str  # Unique locating substring
    invalidated_by: str | None = None  # Replacer memory id (optional)


class ReconcileOperation(BaseModel):
    """A single memory-reconcile operation (memory system v2 P2).

    op semantics (submitted by the agent LLM after fine judgment; the tool only applies them deterministically):
    - merge: content + memo_ids → create a new memo and invalidate each merged entry (invalidated_by = the new entry)
    - invalidate: memo_ids → invalidate item by item (contradictory / overturned)
    - score: memo_id + quality_score(1-10) + reason → fill in the quality score (reason goes into meta)
    - promote: content + kind + source_refs → create a direction-layer entry (the volume red line still applies)
    - keep / noop: do nothing (may be omitted from the submission)
    """

    op: str  # merge / invalidate / score / promote / keep / noop
    content: str = ""  # New content for merge/promote
    memo_ids: list[str] = Field(default_factory=list)  # Target memos for merge/invalidate
    memo_id: str = ""  # Target memo for score
    quality_score: int | None = None  # score: 1-10
    reason: str = ""  # Scoring reason for score
    kind: str = "preference"  # Direction-layer kind for promote
    scope: str = "project"  # Direction-layer scope for promote
    source_refs: list[str] = Field(default_factory=list)  # Provenance ids for promote
    memo_type: str = "summary"  # memo_type of the new entry created by merge
    scope_path: str = ""  # scope_path of the new entry created by merge


class ReconcileApply(BaseModel):
    """Memory-reconcile batch apply request."""

    operations: list[ReconcileOperation] = Field(default_factory=list)


class MeetingMessageCreate(BaseModel):
    """Create meeting message request."""

    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    caller_agent_id: str = ""  # actual caller; if differs from agent_id → impersonation audit


class MeetingConcludeBody(BaseModel):
    """Conclude meeting request body."""

    summary: str = ""
    validate_attendance: bool = True
    force: bool = False


class ChannelMessageCreate(BaseModel):
    """Send channel message request."""

    sender: str
    content: str
    mentions: list[str] = Field(default_factory=list)  # e.g. ["@agent-name", "@team-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
