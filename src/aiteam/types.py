"""AI Team OS — Global shared type definitions.

All modules reference types from this file; they do not define their own data models.
This file is managed by the tech-lead; other engineers only read-reference it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from aiteam.clock import utc_now

# ============================================================
# Enum types
# ============================================================


class OrchestrationMode(enum.StrEnum):
    """Team orchestration mode."""

    COORDINATE = "coordinate"
    BROADCAST = "broadcast"
    ROUTE = "route"
    MEET = "meet"


class TaskStatus(enum.StrEnum):
    """Task status."""

    PENDING = "pending"
    BLOCKED = "blocked"  # Has unfinished dependencies
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(enum.StrEnum):
    """Agent status — three-state model."""

    BUSY = "busy"  # Working — actively executing tool calls
    WAITING = "waiting"  # Waiting — alive but awaiting input (between turns)
    OFFLINE = "offline"  # Offline — terminated


class MeetingStatus(enum.StrEnum):
    """Meeting status."""

    ACTIVE = "active"
    CONCLUDED = "concluded"


class PhaseStatus(enum.StrEnum):
    """Phase status."""

    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TeamStatus(enum.StrEnum):
    """Team lifecycle status."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class MeetingTemplate(enum.StrEnum):
    """Meeting template type."""

    BRAINSTORM = "brainstorm"  # Brainstorming (4 rounds)
    DECISION = "decision"  # Decision meeting (3 rounds)
    REVIEW = "review"  # Review meeting (3 rounds)
    RETROSPECTIVE = "retrospective"  # Retrospective meeting (3 rounds)
    STANDUP = "standup"  # Standup (1 round)
    DEBATE = "debate"  # Debate mode
    LEAN_COFFEE = "lean_coffee"  # Lean Coffee
    FREE = "free"  # Free discussion (default)


class TaskPriority(enum.StrEnum):
    """Task priority."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskHorizon(enum.StrEnum):
    """Task time horizon."""

    SHORT = "short"
    MID = "mid"
    LONG = "long"


class MemoryScope(enum.StrEnum):
    """Memory scope.

    The direction layer (memory system v2 P1) is semantically narrowed to global / project / user — cross-task,
    long-lived
    preferences/corrections/constraints/design intent. Records at task level that affect only a single task belong to
    the episodic layer and go through task_memos.
    TEAM / AGENT are legacy partitions (team knowledge base / agent experience) and do not belong to the direction
    layer.
    """

    GLOBAL = "global"
    PROJECT = "project"
    TEAM = "team"
    AGENT = "agent"
    USER = "user"


class EventType(enum.StrEnum):
    """System event type."""

    # Team events
    TEAM_CREATED = "team.created"
    TEAM_DELETED = "team.deleted"
    TEAM_MODE_CHANGED = "team.mode_changed"
    # a completed team is automatically revived by a new member registration / session resumption (hook_translator
    # auto-revive;
    # recorded 2026-07-22: the precedent _resolve_cc_team had been emitting this event all along while the enum member
    # was absent = a latent ValueError)
    TEAM_AUTO_REVIVED = "team.auto_revived"
    # an expired session container team is swept away by the reaper (distinguished from a user manually deleting a team,
    # for later tracing of who deleted it)
    TEAM_CONTAINER_PURGED = "team.container_purged"

    # Agent events
    AGENT_CREATED = "agent.created"
    AGENT_REMOVED = "agent.removed"
    AGENT_STATUS_CHANGED = "agent.status_changed"

    # Task events
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # Failure-diagnosis trace: if the analysis ran but the result only goes back to the caller and never enters the
    # event stream, it is as if it never ran —
    # afterwards you can neither check whether the diagnosis was done nor count diagnosis coverage.
    TASK_FAILURE_ANALYZED = "task.failure_analyzed"
    TASK_FAILURE_DIAGNOSED = "task.failure_diagnosed"

    # Memory events
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_ACCESSED = "memory.accessed"

    # Meeting events
    MEETING_STARTED = "meeting.started"
    MEETING_MESSAGE = "meeting.message"
    MEETING_ROUND_COMPLETED = "meeting.round_completed"
    MEETING_CONCLUDED = "meeting.concluded"

    # Hook/CC events
    AGENT_AUTO_REGISTERED = "agent.auto_registered"
    CC_TOOL_USE = "cc.tool_use"
    CC_TOOL_COMPLETE = "cc.tool_complete"
    CC_SESSION_START = "cc.session_start"
    CC_SESSION_END = "cc.session_end"
    # CC's own teammate idle signal (the TeammateIdle hook). Observe only, never change state — CC's idle
    # means "this round has finished speaking", while the OS's offline means "the person is gone"; the two cannot be
    # equated.
    CC_TEAMMATE_IDLE = "cc.teammate_idle"
    # A read-only mirror of CC teammate direct messages (SendMessage). Deliberately not written into channel_messages —
    # that table is the territory of the OS's own broadcast channel (planned but not enabled); the mirror goes only into
    # the event stream.
    CC_MESSAGE_SENT = "cc.message_sent"
    # Dual-track liveness-criterion observation (C13): when the transcript mtime and the CC session registry disagree,
    # record one entry, for a later decision on whether to switch the primary criterion. The judgment itself still comes
    # from mtime alone.
    SESSION_LIVENESS_DIVERGENCE = "session.liveness_divergence"
    # Compaction checkpoint: PreCompact freezes the OS-side operational state, and the post-compaction SessionStart
    # hands it back.
    SESSION_COMPACT_CHECKPOINT = "session.compact_checkpoint"
    # PostCompact receipt: compaction really happened (after a PreCompact trigger, compaction can still be cancelled).
    SESSION_COMPACT_COMPLETED = "session.compact_completed"
    # The Leader main-session usage collection raised an exception. Recorded only on **genuine anomalies** (throttling
    # skips never enter this table) —
    # logs get truncated on process restart but events do not, so afterwards a single SQL query can answer "did that
    # PostCompact
    # actually capture anything" (the 2026-08-03 investigation spent two hours unable to answer it, hence this trace).
    LEADER_USAGE_CAPTURE_FAILED = "leader.usage_capture_failed"
    # Isolated workspace birth/disappearance (this repo's multi-session parallel discipline requires git worktree
    # isolation; the OS was previously unaware)
    CC_WORKTREE_CREATED = "cc.worktree_created"
    CC_WORKTREE_REMOVED = "cc.worktree_removed"
    # The observation surface for CC-native tasks. The bridge (cc_task_bridge) does bookkeeping only on TaskCompleted,
    # and that
    # event previously had no telemetry attached alongside, so "how many times the bridge fired and how many entries it
    # filtered" had no evidence. These three events
    # **only observe, never bookkeep**; the on-wall logic still belongs to the bridge.
    CC_TASK_CREATED = "cc.task_created"
    CC_TASK_COMPLETED = "cc.task_completed"
    # On the abort side CC provides no hook (there is no TaskStop/TaskAborted event, and an Esc interruption is silent
    # too),
    # so the only option is to lift the TaskStop **tool call** out of the tool-event flood into a first-class event —
    # measured in practice, aborts are
    # the main path (TaskStop 40 times and still in use; TaskCreate 14 times and long since zero).
    CC_TASK_STOPPED = "cc.task_stopped"

    # Hourly aggregate rows of the HTTP request-level ledger (api/request_ledger.py). They give the "zero calls"
    # judgment a second
    # caliber: calls invisible to the MCP tool surface (Dashboard/hooks/scripts/other sessions) are all here.
    API_REQUEST_ROLLUP = "api.request_rollup"

    # File events
    FILE_EDIT_CONFLICT = "file.edit_conflict"

    # Task lifecycle events
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_ASSIGNED = "task.assigned"

    # Task dependency events
    TASK_DECOMPOSED = "task.decomposed"
    TASK_BLOCKED = "task.blocked"
    TASK_UNBLOCKED = "task.unblocked"

    # System events
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    SYSTEM_ERROR = "system.error"

    # Decision events (TOP2 cockpit — unified decision event stream)
    DECISION_TASK_ASSIGNED = "decision.task_assigned"
    DECISION_APPROACH_CHOSEN = "decision.approach_chosen"
    DECISION_AGENT_SELECTED = "decision.agent_selected"
    DECISION_AGENT_CREATED = "decision.agent_created"
    DECISION_MEETING_STARTED = "decision.meeting_started"
    # The original text of decision scenes (low-frequency, high-value — the exact opposite of the heartbeats whose
    # writing was stopped):
    # ExitPlanMode's plan body / AskUserQuestion's question-answer pairs
    DECISION_PLAN_PRESENTED = "decision.plan_presented"
    DECISION_USER_ASKED = "decision.user_asked"

    # Knowledge events
    KNOWLEDGE_LESSON_LEARNED = "knowledge.lesson_learned"

    # Intent events
    INTENT_AGENT_WORKING = "intent.agent_working"

    # Enhanced event log (v0.9) — generic update events with state snapshots
    TASK_UPDATED = "task.updated"
    AGENT_UPDATED = "agent.updated"

    # Channel events (v1.0 P1-6)
    CHANNEL_MESSAGE = "channel.message"

    # Workflow observability events (I3a — CC ultracode/Workflow observation layer)
    # append-only: once historical data is written it can never be deleted (the read side's EventType(x) would crash).
    WORKFLOW_PLANNED = "workflow.planned"  # PreToolUse(Workflow) static plan ready
    WORKFLOW_STARTED = "workflow.started"  # PostToolUse(Workflow) receipt skeleton ready
    WORKFLOW_COMPLETED = "workflow.completed"  # file reconciliation lands the final telemetry
    # Phase 2 live tracking (fulfilling the reservation above; aggregated per run per tick, never emitted per agent)
    WORKFLOW_AGENT_UPDATED = "workflow.agent_updated"  # live tail: this tick has agent increments
    WORKFLOW_RUN_INGESTED = "workflow.run_ingested"  # run-level live watermark / killed·failed first terminal state /
    # interrupted marking

    # Progressive tool loading P1 — alwaysLoad dynamic rotation audit (one row recorded per recomputation during session
    # startup;
    # that row is also the next period's hysteresis baseline, unifying state and audit. append-only; after historical
    # writes it cannot be deleted.)
    TOOL_ALWAYSLOAD_ROTATION = "tool.alwaysload.rotation"

    # Governance lease change of hands (A2-obs, debate 503e07f1 topic A): the previous holder still hangs on the row
    # with its lease expired,
    # and another instance snatches it. Emitted only on this branch — renewals, ownerless claims, and takeovers after an
    # active yield do not count as handovers.
    # The purpose is first to answer "has this ever happened": if there is not a single one in the observation period,
    # A2-impl (the epoch column) need not be done.
    GOVERNANCE_LEASE_TAKEN_OVER = "governance.lease_taken_over"


# ============================================================
# Token calibers (token usage attribution v1, stage 0 "caliber rectification")
# ============================================================
# A token number without a caliber is meaningless. This DB holds two orthogonal token calibers simultaneously, differing
# by a measured
# 5~25× (the same batch of agents: 117,800 vs 632,504 … 127,089 vs 3,236,088). Adding them,
# juxtaposing them, or stuffing them into one "total tokens" is the same class of accident this repo just suffered with
# timestamps — caliber mixing.
# See docs/token-attribution-v1-design.md §0.2 / §1.1 for the spec.


class TokenMetric(enum.StrEnum):
    """The caliber of a token number — a first-class dimension of the attribution layer, on par with coverage.

    There are only these two members because only these two calibers answer the "usage" question. Value semantics:

    * ``USAGE_SUM``: the four-layer tokens obtained by grouping one transcript by ``requestId``, taking the last usage
    snapshot
      of each group, then accumulating across groups. Answers "how much did this work use in total".
    * ``CTX_LAST``: the sum of the four fields of the last assistant message — an **instantaneous context watermark
    snapshot**,
      not a consumption amount. Answers "how full was this agent's context when it ended".

    The two are **never added together**.
    """

    USAGE_SUM = "usage_sum"
    CTX_LAST = "ctx_last"


# A third existing token caliber: the context watermark (``agents.ctx_tokens`` / ``ctx_pct``, produced by
# ``agent_context.measure``, serving agent reuse governance). It is **deliberately not a TokenMetric
# member** — TokenMetric covers the single problem domain of "usage attribution", while the watermark caliber is
# unrelated to usage (§1.1).
# But it still needs a labelable name: every token number on the presentation surface must carry a caliber, including
# those not participating in attribution.
# The three calibers are never added to each other.
CTX_WATERMARK_METRIC = "ctx_watermark"

# All legitimate caliber labels — the closed set shared by presentation-surface labeling and machine checks (I13).
TOKEN_METRIC_LABELS: frozenset[str] = frozenset(
    {TokenMetric.USAGE_SUM.value, TokenMetric.CTX_LAST.value, CTX_WATERMARK_METRIC}
)

# caliber → (short name, producer, one-sentence definition). The presentation surface's caliber badges and footer notes
# are taken from here,
# avoiding the same explanation being written once each in pages, docs, and comments and then drifting apart.
TOKEN_METRIC_SPECS: dict[str, tuple[str, str, str]] = {
    TokenMetric.USAGE_SUM.value: (
        "Usage total",
        "services/token_attribution.parse_transcript_usage → the agents five columns",
        "Four-layer tokens accumulated across groups after taking each group's last snapshot by requestId; answers how "
        "much was used in total.",
    ),
    TokenMetric.CTX_LAST.value: (
        "Final-round context watermark",
        "api/workflow_ingest._last_assistant_ctx_tokens → workflow_agents.tokens",
        "The sum of the four fields of the last assistant message; an instantaneous watermark snapshot, not a "
        "consumption amount.",
    ),
    CTX_WATERMARK_METRIC: (
        "Context watermark (reuse governance)",
        "api/agent_context.measure → agents.ctx_tokens / ctx_pct",
        "How much context an agent currently occupies; serves reuse decisions and is unrelated to usage attribution.",
    ),
}

# The four layers of token usage — a measured 95.6% of the "total" is cache_read, so reporting only the total equals
# reporting only the cache-read amount,
# and cross-model/cross-dispatch-path comparisons would be systematically skewed. The four layers must therefore always
# be separately presentable (§1.2).
TOKEN_LAYERS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)


class TokenSource(enum.StrEnum):
    """The values of ``agents.tokens_source`` — how this row's token numbers came to be.

    A NULL column means **not yet collected** — neither transcript-verified nor alias-fallback-derived — and no-data and
    zero must be distinguishable (§2.6).
    """

    TRANSCRIPT = "transcript"  # parsed line by line from the transcript and verified
    ALIAS_FALLBACK = "alias_fallback"  # the transcript is lost; derived on the read side via the alias ledger fallback


class AttributionScope(enum.StrEnum):
    """One attribution query asks "which segment of work".

    The first four levels go down the attribution chain from the top (§2.1), and each level's denominator is **the
    dispatch count within that level**, not
    "the measured dispatch count within that level" — the latter would make coverage always 100% (§4.1).

    ``TASK`` is a side branch, not a fifth level: task edges are collected parasitically on bookkeeping actions (§2.4),
    with a measured coverage
    close to 0 — not the same order of credibility as the first four levels, and any presentation must label it
    separately.
    """

    PROJECT = "project"
    SESSION = "session"
    WORKFLOW_RUN = "workflow_run"
    AGENT = "agent"
    TASK = "task"


class AttributionMethod(enum.StrEnum):
    """How this attribution's numbers **came to be** — in a row-level/query-level relationship with ``TokenSource``.

    ``TokenSource`` records "where an agents row's four-layer numbers come from" and persists with the row; this enum
    records
    "which path this aggregation query as a whole took" and returns with the query result. The two share names but live
    at different levels — do not mix them.
    """

    TRANSCRIPT_PARSE = "transcript_parse"  # parsed line by line from the transcript and verified (the usage_sum main
    # path)
    SELF_REPORT = "self_report"  # self-reported values from the workflow JSON / journal (the ctx_last main path)
    ALIAS_FALLBACK = "alias_fallback"  # the transcript is lost; read-side fallback via the alias ledger


class UnattributedReason(enum.StrEnum):
    """Unattributed is not one state but several, each handled differently (§3.4).

    This table's sole reason to exist is **to turn "78% coverage" into an actionable statement** — the reader should be
    able to tell from it
    whether the remaining 22% can be rescued. So "unrecoverable" and "not yet attempted" must be labeled separately;
    merging the former into the latter
    would make this enum lose all its value.

    The first four are §3.4's original table. The last two are named extensions, because §3.4's table was written for
    usage_sum's sub-
    agent path, and force-fitting it onto the other two paths would lie:

    * ``SELF_REPORT_ABSENT`` — when the ctx_last side's ``workflow_agents.tokens`` is 0,
      the reason is that run's JSON never carried telemetry from the start (the data source holds only request specs).
      It is not
      ``NOT_YET_MEASURED``: no "run collection once more" can recover it.
    * ``MULTI_TASK_UNSPLITTABLE`` — when an agent left bookkeeping entries on multiple tasks, its four-layer numbers are
      **a single total over its whole lifecycle**, with no per-interval usage to split by edge in time order. §2.4 fixes
      that this case is honestly counted as unattributed, with **averaging allocation forbidden** (allocation would
      fabricate unfalsifiable numbers).

    ``BY_DESIGN`` is §3.4's fourth code, but it describes the **tool-call level** (``agent_activities``'
    52,119 records have no token field — a deliberate choice not to collect). Tool calls and dispatches are not the same
    unit, so it
    serves only as a one-line label on the presentation surface and **never enters**
    :attr:`TokenAttribution.unattributed_reasons`
    — that dict is in the same unit as ``dispatches_total``, and mixing activity counts into it would mix dimensions.
    """

    NO_TRANSCRIPT_PATH = "no_transcript_path"  # the row never registered a transcript path (historical row,
    # unrecoverable)
    TRANSCRIPT_GONE = "transcript_gone"  # the path exists but the file is gone (only grows over time, unrecoverable)
    NOT_YET_MEASURED = "not_yet_measured"  # path present, file present, collection just has not run yet (recoverable)
    BY_DESIGN = "by_design"  # not collected by design (tool-call level) — see the class docs; does not enter the dict
    SELF_REPORT_ABSENT = "self_report_absent"  # the ctx_last side's self-reported value is absent (unrecoverable)
    MULTI_TASK_UNSPLITTABLE = "multi_task_unsplittable"  # cannot be split at task level; honestly counted as
    # unattributed


class TokenAttribution(BaseModel):
    """The complete answer to one attribution query — the numbers live and die with their denominator and caliber.

    There is no ``total`` field, and no method returns one. To render a number, a caller must simultaneously
    obtain ``dispatches_total`` and ``metric`` — the denominator and caliber are part of the data, not optional
    decoration. This **must live at the type layer**: page labeling is a soft constraint that one's future self three
    months later will ignore
    (§2.5; this structure is pinned by the I13 machine check).

    **Why there is no total field**: a measured 95.6% of the four layers is ``cache_read``, so any "total" is actually
    a synonym for "cache-read amount"; reporting only the total would systematically skew cross-model and
    cross-dispatch-path comparisons
    (§1.2). The four layers must always be separately presentable; if a total is computed, the caller computes it and
    bears the interpretation responsibility.

    **The caliber (metric) having no default** is deliberate: without labeling the caliber, the object cannot be
    constructed. This DB holds both
    ``usage_sum`` (usage total) and ``ctx_last`` (final-round context watermark), two orthogonal calibers differing by a
    measured
    5~25×; adding or juxtaposing them is the same class of accident this repo just suffered with timestamps (§0.2 / R1).
    This structure can carry both calibers; **the basis of judgment is always the metric field itself**, not field
    names.

    Invariants (pinned by contract tests, see tests/unit/test_token_attribution_contract.py):

    * no ``total`` / ``total_tokens`` field or attribute exists;
    * ``dispatches_attributed`` + Σ``unattributed_reasons`` == ``dispatches_total``;
    * ``unattributed_reasons`` is in units of **dispatch count**, the same unit as the denominator.
    """

    model_config = {"frozen": True}

    scope: AttributionScope  # project / session / workflow_run / agent / task
    scope_id: str
    metric: TokenMetric  # usage_sum | ctx_last — mandatory labeling, no default
    input_tokens: int  # four layers presented separately; no total field provided
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    dispatches_attributed: int  # numerator: dispatches within this scope whose usage has been measured
    dispatches_total: int  # denominator: the total dispatch count within this scope
    unattributed_reasons: dict[str, int]  # unattributed dispatches counted by reason category (§3.4)
    measured_window: tuple[datetime, datetime] | None
    method: AttributionMethod  # transcript_parse | self_report | alias_fallback


class DispatchPopulation(enum.StrEnum):
    """Dispatch path — a row of the coverage matrix (§5.2), and one of the aggregation query's required dimensions.

    Why it must be a query dimension rather than a post-hoc grouping: the three paths' magnitudes differ by orders of
    magnitude (measured: a single
    Leader session at 850M tokens, far exceeding the sum of all sub-agents); mixed into one leaderboard, sub-agents'
    attribution results would be completely drowned. §3.3 therefore fixes that "the main session and sub-agents must be
    presented in separate columns, and by default
    not merged" — making it a required parameter means merging requires explicitly issuing two queries first and then
    adding by hand, and the person adding
    bears that action themselves.
    """

    SUBAGENT = "subagent"  # agents table role != 'leader' — dispatches (usage_sum)
    LEADER_SESSION = "leader_session"  # agents table role == 'leader' — main sessions (usage_sum)
    WORKFLOW_SELF_REPORT = "workflow_self_report"  # workflow_agents self-report (ctx_last, coverage only)
    TOOL_CALL = "tool_call"  # agent_activities tool-call level — not collected by design (§3.2)


class UsageCoverageRow(BaseModel):
    """One row of the coverage matrix — numerator, denominator, caliber, with **deliberately zero token value fields**.

    The two tables in §4.3 and §5.2 are coverage tables to begin with: their columns are "dispatch count / measured /
    coverage / caliber",
    and not one column is a token number. This lets the ``ctx_last`` path also appear honestly — its four-layer
    decomposition was
    never stored (``workflow_agents.tokens`` is already the sum of the four fields at ingest time), so it cannot enter
    :class:`TokenAttribution`, but its numerator and denominator are real.

    ``dispatches_total`` / ``dispatches_attributed`` may be None, specifically for the "not collected by design"
    row: **a blank would be read as a bug, and 0 would be read as "truly never once"**, while in fact this question
    does not apply on this path (§5.2: "not collected by design" is a formal value, not a blank).
    """

    path: DispatchPopulation
    metric: str  # a TokenMetric value or "" (this path produces no usage numbers)
    dispatches_total: int | None
    dispatches_attributed: int | None
    unattributed_reasons: dict[str, int] = Field(default_factory=dict)
    note: str = ""


class EdgeCoverage(BaseModel):
    """The resolvability rate C_hop of one hop on the attribution chain (§4.1).

    End-to-end coverage is the product of the hops; expressing it as one scalar would hide the real bottleneck —
    measured: the bottleneck is not in
    token collection but in the ``agent→task`` hop (0.8%), which total coverage alone would never reveal.
    """

    edge: str  # e.g. "agent->session"
    resolvable: int  # numerator: the number of rows for which this edge is resolvable
    required: int  # denominator: the number of rows needing this edge
    note: str = ""


class UsageCoverageReport(BaseModel):
    """The complete answer of the coverage vector — the data source of the page's first block on the first screen (§5.2
    items 1-2).

    Deliberately **contains no token values**: this structure answers "what proportion was measured", and token values
    uniformly go through
    :class:`TokenAttribution` (where every number carries its own denominator). Therefore it does not enter
    ``scripts/usage_surface.py``'s PY_SURFACES — that registry governs token presentation surfaces,
    while here there is not a single token field.

    ``rows`` deliberately provides no total row: the ``usage_sum`` and ``ctx_last`` calibers differ by a measured 5~25×,
    and any cross-row total mixes calibers (§0.2 / §5.2).
    """

    rows: list[UsageCoverageRow]
    hops: list[EdgeCoverage]
    window: tuple[datetime, datetime] | None  # windowed on created_at, not tokens_measured_at
    generated_at: datetime = Field(default_factory=utc_now)


# ============================================================
# Data models
# ============================================================


def _new_id() -> str:
    return str(uuid4())


class Project(BaseModel):
    """Project data model."""

    id: str = Field(default_factory=_new_id)
    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Phase(BaseModel):
    """Phase data model — execution phase under a Project."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    name: str
    description: str = ""
    status: PhaseStatus = PhaseStatus.PLANNING
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Team(BaseModel):
    """Team data model."""

    id: str = Field(default_factory=_new_id)
    name: str
    mode: OrchestrationMode = OrchestrationMode.COORDINATE
    project_id: str | None = None
    leader_agent_id: str | None = None  # Leader agent for this team
    status: TeamStatus = TeamStatus.ACTIVE
    summary: str = ""  # One-line summary after team completion
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    # The CC process owning this container team. **A derived field, not persisted**: resolved by the API at response
    # time, for the display layer
    # to combine the same process's history + current container teams into one group. When it cannot be proven it is
    # None (never guess); non-container
    # teams are always None.
    cc_pid: int | None = None


class Agent(BaseModel):
    """Agent data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    name: str
    role: str
    system_prompt: str = ""
    # When the model is unknown, leave it empty (displayed as --): baking a specific model in as the default once
    # surfaced misleading displays at four layers (here / the ORM column default /
    # to_pydantic read injection / tool parameters); the real value is backfilled by transcript tail-reads
    # (Leader) / wf terminal state (workflow agents).
    model: str = ""
    status: AgentStatus = AgentStatus.WAITING
    config: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"  # "api" = registered via CLAUDE.md, "hook" = auto-captured by hooks
    session_id: str | None = None  # Associated CC session ID
    cc_tool_use_id: str | None = None  # Associated CC internal agent ID
    current_task: str | None = None  # Currently executing task/activity description
    project_id: str | None = None
    current_phase_id: str | None = None
    trust_score: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)
    last_active_at: datetime | None = None
    # Agent reuse governance P1 (batch 1B): sub-agent context watermark ledger.
    # Populated from the sub-agent transcript on SubagentStop + reaper backfill;
    # reuse_domain is provisioned for the P2 decision layer (not written in P1).
    # See docs/agent-reuse-design.md section 4.
    # Caliber: CTX_WATERMARK_METRIC (context watermark; does not participate in usage attribution and is not added to
    # the four layers below)
    ctx_tokens: int | None = None  # last measured context token total (D1 formula)
    ctx_window: int | None = None  # detected window size (e.g. 1_000_000)
    ctx_pct: float | None = None  # ctx_tokens / ctx_window * 100
    transcript_path: str | None = None  # sub-agent transcript pointer (resume/re-read anchor)
    ctx_measured_at: datetime | None = None  # when the watermark was last measured
    reuse_domain: str | None = None  # most-recent task domain tag (P2 decision layer)
    # Caliber: TokenMetric.USAGE_SUM (usage total, four layers presented separately). A different thing from the ctx_*
    # watermark above;
    # the two are not added. None = not yet collected, not equal to 0 — no-data and zero must be distinguishable.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    tokens_measured_at: datetime | None = None
    # How this row's four-layer numbers came to be: transcript-verified / alias fallback. None = not collected.
    # Audit provenance only; participates in no computation (§2.6, the only column this design adds).
    tokens_source: TokenSource | None = None


class Task(BaseModel):
    """Task data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str | None = None
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str | None = None
    result: str | None = None
    parent_id: str | None = None
    project_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    depth: int = 0
    order: int = 0
    template_id: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    horizon: TaskHorizon = TaskHorizon.SHORT
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    # The id of a CC-native task (the task_id in the TaskCompleted payload). Only rows mirrored in by cc_task_bridge
    # have a value; it is the mirror's idempotency key — the same CC task completing repeatedly will not create a second
    # row.
    cc_task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Memory(BaseModel):
    """Memory data model.

    Direction layer (memory system v2 P1): low-frequency, high value-density, cross-task long-lived
    preferences/corrections/constraints/
    design intent. Scope semantics: global/project/user; on contradiction/update, supersedes invalidates the old entry
    (Zep invalidation semantics, no deletion). source_refs point back to memo/report/meeting ids (item 4 provenance).
    """

    id: str = Field(default_factory=_new_id)
    scope: MemoryScope
    scope_id: str
    content: str
    # preference / directive (working style) / constraint / design (design intent)
    kind: str = "preference"
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)  # item 4 provenance: memo/report/meeting id
    invalid_at: datetime | None = None  # item 1 invalidation axis (NULL = valid)
    invalidated_by: str | None = None  # the superseding memory id
    created_at: datetime = Field(default_factory=utc_now)
    accessed_at: datetime = Field(default_factory=utc_now)


class TaskMemo(BaseModel):
    """Episodic-layer task memo (memory system v2 P0: promoted from a tasks.config JSON array to its own table).

    A row-level real ID (referenceable by knowledge_links) + an invalidation axis (invalid_at/invalidated_by,
    Zep invalidation semantics: on contradiction, invalidate without deleting). The write interface stays compatible;
    fields align with design §2.
    """

    id: str = Field(default_factory=_new_id)
    task_id: str
    project_id: str | None = None
    author: str = "leader"
    memo_type: str = "progress"  # progress / decision / issue / summary
    content: str
    scope_path: str = ""  # item 2 path scope /project/ecosystem/research
    quality_score: int | None = None  # item 8 quality score (NULL = not yet rated; filled in during organization)
    invalid_at: datetime | None = None  # item 1 invalidation axis (NULL = valid)
    invalidated_by: str | None = None  # the superseding memo id
    meta: dict[str, Any] = Field(default_factory=dict)  # entities/topics (filled in during organization)
    created_at: datetime = Field(default_factory=utc_now)


class Event(BaseModel):
    """System event data model."""

    id: str = Field(default_factory=_new_id)
    type: EventType
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)
    # Enhanced event context (v0.9)
    entity_id: str | None = None    # ID of the primary entity involved (task/agent/team)
    entity_type: str | None = None  # Entity type: "task" / "agent" / "team" / "meeting"
    state_snapshot: dict[str, Any] | None = None  # Trimmed key fields at event time


class Meeting(BaseModel):
    """Meeting data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    topic: str
    status: MeetingStatus = MeetingStatus.ACTIVE
    participants: list[str] = Field(default_factory=list)
    project_id: str | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    concluded_at: datetime | None = None


class MeetingMessage(BaseModel):
    """Meeting message data model."""

    id: str = Field(default_factory=_new_id)
    meeting_id: str
    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    timestamp: datetime = Field(default_factory=utc_now)
    msg_metadata: dict[str, Any] = Field(default_factory=dict)  # audit: impersonation, actual_author, etc.


class AgentActivity(BaseModel):
    """Agent activity record — logs each agent tool call."""

    id: str = Field(default_factory=_new_id)
    agent_id: str
    session_id: str
    tool_name: str  # Tool name (Bash, Edit, Read, Agent, etc.)
    input_summary: str = ""  # Input summary (e.g. command, file path)
    output_summary: str = ""  # Output summary (truncated to 500 chars)
    timestamp: datetime = Field(default_factory=utc_now)
    duration_ms: int | None = None  # Tool call duration (ms), populated by Pre->Post correlation
    status: str = "completed"  # "running" | "completed" | "error"
    error: str | None = None  # Error message


class CrossMessageType(enum.StrEnum):
    """Cross-project message type."""

    NOTIFICATION = "notification"
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"


class CrossMessage(BaseModel):
    """Cross-project message — shared across all projects in the global DB."""

    id: str = Field(default_factory=_new_id)
    from_project_id: str
    from_project_dir: str
    to_project_id: str | None = None  # None means broadcast to all projects
    sender_name: str
    content: str
    message_type: CrossMessageType = CrossMessageType.NOTIFICATION
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    read_at: datetime | None = None


class ScheduledTask(BaseModel):
    """Scheduled task — periodic automation trigger."""

    id: str = Field(default_factory=_new_id)
    team_id: str | None = None
    name: str
    description: str = ""
    interval_seconds: int  # minimum 300 (5 min)
    action_type: str  # create_task / inject_reminder / emit_event
    action_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class WakeSession(BaseModel):
    """Record of a single wake_agent subprocess execution."""

    id: str = Field(default_factory=_new_id)
    scheduled_task_id: str
    agent_name: str
    team_id: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    outcome: str = ""  # completed / skipped_triage / timeout / error / fused / skipped_concurrent
    triage_result: str = ""
    stdout_summary: str = ""  # last 500 chars
    exit_code: int | None = None
    consecutive_failures: int = 0
    duration_seconds: float = 0.0


class LeaderBriefing(BaseModel):
    """Leader Briefing — pending decision items for user review."""

    id: str = Field(default_factory=_new_id)
    title: str
    description: str = ""
    options: str = ""  # A/B/C options description
    recommendation: str = ""  # Leader's suggested option
    urgency: str = "medium"  # high / medium / low
    status: str = "pending"  # pending / resolved / dismissed
    resolution: str = ""  # user's decision
    project_id: str = ""
    tags: list[str] = Field(default_factory=list)  # free-form, for filtering the queue
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class Report(BaseModel):
    """Research/analysis report — stored in database with project isolation."""

    id: str = Field(default_factory=_new_id)
    project_id: str = ""
    author: str = ""
    topic: str = ""
    report_type: str = "research"  # research / design / analysis / meeting-minutes
    date: str = ""  # YYYY-MM-DD
    content: str = ""
    task_id: str = ""
    team_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowRun(BaseModel):
    """Workflow run profile — the queryable projection of one CC ultracode/Workflow run.

    Positioning: the "rebuildable cache" of the `wf_<id>.json` rich snapshot, advanced monotonically by UPSERT on the
    natural key `wf_id`
    (planned→running→completed / interrupted), never deleting rows. The audit trail still goes through the events table.
    """

    id: str = Field(default_factory=_new_id)
    wf_id: str  # wf_<id>, the idempotency primary anchor
    project_id: str = ""  # bound to the launching Leader's project; goes through _apply_project_filter
    team_id: str | None = None  # the existing workflow-<wf_id> team; None when there is no team during the OS's offline
    # period
    session_id: str | None = None  # the launching Leader session
    cc_task_id: str | None = None  # the Task ID in the receipt (≠ OS task_id)
    name: str = ""  # the run name (receipt/script meta)
    status: str = "planned"  # planned / running / completed / interrupted / killed / failed
    source: str = "hook"  # data-plane provenance: hook / file / hook+file
    phases: list[dict[str, Any]] = Field(default_factory=list)  # [{index,title}]
    planned_agent_count: int = 0  # statically parsed literal_agent_count
    dynamic_nodes: int = 0  # statically parsed dynamic node count
    agent_count: int = 0  # actual (snapshot agentCount)
    # Caliber: TokenMetric.CTX_LAST — the snapshot totalTokens = Σ each agent's final-round context watermark,
    # **not** how much this run burned in total. It differs from the agents five columns (USAGE_SUM) by a measured
    # 5~25×,
    # and is never added to them. This stage only rectifies names; data and algorithms are untouched (§5.3 / §6.4-1).
    total_tokens: int = 0  # snapshot totalTokens
    total_tool_calls: int = 0  # snapshot totalToolCalls
    duration_ms: int | None = None  # snapshot durationMs
    summary: str = ""  # the run result summary
    result: dict[str, Any] | None = None  # terminal StructuredOutput (truncated, anti-bloat)
    script_path: str = ""  # the script .js path, for drill-down
    # Cross-project fix A: the receipt's Transcript dir is persisted — live/terminal states address it directly,
    # escaping the "the project must
    # be registered" dependency (runs of unregistered projects were once misjudged as interrupted / fully blind while
    # live because the slug could not be scanned).
    transcript_dir: str = ""
    started_at: datetime | None = None  # startTime
    completed_at: datetime | None = None  # startTime + durationMs
    # Phase2 live watermark columns — None = this upsert does not change them; explicit 0/'' = reset (watermark
    # semantics,
    # see the separate branch in repository.upsert_workflow_run; never apply "new non-zero wins").
    journal_offset: int | None = None  # the consumed-byte watermark of journal.jsonl (advances only to the last \n)
    source_fingerprint: str | None = None  # wf_<id>.json's "mtime_ns:size"; reconcile skips cheaply
    # Caliber: TokenMetric.CTX_LAST (same as total_tokens, just the running-period version of the estimate)
    live_tokens: int | None = None  # running-period estimate = Σ agents lastCtx (cached counts as 0); the
    # terminal-state UI uses total_tokens
    last_activity_at: datetime | None = None  # max(journal + agent jsonl mtime); monotonically takes max
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeLink(BaseModel):
    """Cross-domain reference edge — knowledge layer P1a (docs/knowledge-layer-design.md).

    Extracts OS-native ID references (wf_id/commit/
    task-uuid/[[memory]]) from memo/report text with zero-LLM regexes; append-only, deduplicated by a UNIQUE quintuple.
    The graph is a derived view,
    rebuildable from the source text at any time (the file-source-of-truth philosophy).
    """

    id: int | None = None  # auto-increment; None before insertion
    from_kind: str  # task_memo / report / task
    from_id: str  # memo: "<task_id>#<ts>"; report/task: uuid
    to_kind: str  # run / task / commit / memory / report
    to_id: str  # wf_id / uuid / short-hash / memory-slug
    link_type: str = "references"  # references / fixes
    context: str = ""  # an evidence snapshot of ±120 characters around the hit point
    link_source: str = ""  # regex-memo / regex-report / manual
    project_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)


# The vocabulary of the ``agent --worked_on--> task`` edge (§2.4). The write side parasitizes the task_memo_add /
# task_update / report_save — three bookkeeping actions that **already necessarily happen**; the read side filters by it
# in attribution aggregation.
# The two sides must share the same literal: write one character wrong and the edge still gets written while the query
# still returns empty,
# with no error whatsoever — it would only manifest as "task-level attribution coverage is forever 0", a code problem
# that looks like a data problem.
AGENT_TASK_LINK_FROM_KIND = "agent"
AGENT_TASK_LINK_TO_KIND = "task"
AGENT_TASK_LINK_TYPE = "worked_on"
# This edge's semantic boundary (written into the code because it is extremely easy to misread): it says "this agent
# left bookkeeping entries on this task", **not** "all of this agent's tokens belong to this task".
AGENT_TASK_LINK_SOURCE = "ledger-parasite"


class WorkflowAgent(BaseModel):
    """Per-agent telemetry — one row per run per fan-out agent.

    upsert by (wf_id, cc_agent_id). The data is 100% ready-made, from
    `wf_<id>.json.workflowProgress[]` type=workflow_agent entries; no self-aggregation needed.
    """

    id: str = Field(default_factory=_new_id)
    run_id: str  # = workflow_runs.wf_id
    wf_id: str  # redundant, for direct querying
    project_id: str = ""  # isolation
    cc_agent_id: str = ""  # the snapshot agentId; forms the unique dedup key together with run_id
    os_agent_id: str | None = None  # links an existing member: agents.cc_tool_use_id == cc_agent_id
    label: str = ""  # e.g. map:mcp
    phase_index: int = 0
    phase_title: str = ""
    model: str = ""  # e.g. claude-opus-4-8[1m]
    state: str = ""  # queued / running / done
    # Caliber: TokenMetric.CTX_LAST — a final-round context watermark snapshot, not this agent's consumption.
    # The same agent's USAGE_SUM is fetched via the os_agent_id association to the agents five columns and **never
    # overwrites this column**
    # (§6.4-1: solve by association, not by overwriting). 0 in this column also means "not captured" — a ctx_last-side
    # historical legacy; this stage touches neither the data nor the algorithm, and I13 has it on record (§5.3).
    tokens: int = 0
    tool_calls: int = 0
    duration_ms: int | None = None
    last_tool_name: str = ""
    last_tool_summary: str = ""
    prompt_preview: str = ""
    result_preview: str = ""
    started_at: datetime | None = None
    queued_at: datetime | None = None
    last_activity_at: datetime | None = None  # Phase2: this agent jsonl's mtime (the swimlane's right end + skip
    # watermark)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StageTransition(BaseModel):
    """Pipeline stage transition event. Stored in its own table pipeline_stage_history (append-only).

    RETAINED (pipeline subsystem retired 2026-07): the pipeline runtime is gone and
    nothing writes this table any more, but the table itself is append-only history
    that must stay readable, so this row schema backs PipelineStageHistoryModel.
    Do not delete — deleting it would break the retained ORM model.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    from_stage: str | None = None
    to_stage: str
    transitioned_at: datetime = Field(default_factory=utc_now)
    triggered_by: Literal["manual", "auto", "force", "system"] = "manual"
    reason: str = ""


class ChannelMessage(BaseModel):
    """Channel message — supports cross-team broadcasting with @mention semantics."""

    id: str = Field(default_factory=_new_id)
    channel: str  # "team:<name>" / "project:<id>" / "global"
    sender: str
    content: str
    mentions: list[str] = Field(default_factory=list)  # ["@agent-name", "@team-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemRepoProfile(BaseModel):
    """Claude ecosystem repo profile — broad index search + periodic updates.

    Project isolation: project_id=None means global/unowned; each project has its own snapshot row.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_full_name: str  # "owner/repo"
    name: str
    owner: str
    description: str | None = None
    stars: int = 0
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    homepage: str | None = None
    last_commit_at: datetime | None = None
    needs_deep_review: bool = False  # True when stars < 15000
    # "agent-framework" / "mcp-server" / "memory-system" / "skill-system" / "tooling"
    relevance_category: str | None = None
    relevance_score: int = 0  # 0-10
    one_line_summary: str | None = None
    last_scanned_at: datetime = Field(default_factory=utc_now)
    first_seen_at: datetime = Field(default_factory=utc_now)
    # Stage B extension fields
    pushed_at: datetime | None = None  # the GitHub repo's last push time, used to judge activity
    is_archived: bool = False  # no push for > 365 days marks it deprecated
    scan_run_id: str | None = None  # associated with the scan batch EcosystemScanRun.id
    description_excerpt: str = ""  # description excerpt, used for secondary relevance filtering
    # v1.5.0-A extension: progressive funnel Stage 0 shallow scan + failure tracking + active set
    shallow_summary: str = ""  # Stage 0 agent shallow-scan summary (200-400 characters, distinct from
    # description_excerpt)
    last_shallow_refreshed_at: datetime | None = None  # the last shallow-scan refresh time
    is_deleted: bool = False  # the repo was deleted on the GitHub side (API 404)
    is_private_now: bool = False  # the repo was made private on the GitHub side (API 403 forbidden, not rate limit)
    last_fetch_error: str = ""  # a short message of the most recent fetch error
    fetch_failure_count: int = 0  # cumulative failure count
    is_active: bool = True  # DEPRECATED v1.6.0 P1.A: use last_active_status instead. This field is kept only for
    # backward compatibility.
    active_rank: int | None = None  # rank within the current project (by stars; None = not in top_n)
    # v1.6.0-P0.4: NormalizedSignal fields (written by index_update)
    canonical_id: str | None = None  # "github/owner/repo" cross-source dedup key
    source_kind: str = "github"  # which data source produced this profile
    last_active_status: str | None = None  # 'active'|'inactive'|'archived'|'manual_archived'
    last_status_change_at: datetime | None = None  # when last_active_status last changed
    popularity_percentile: float | None = None  # 0-1, 1.0 = top of scan results
    activity_score: float | None = None  # 0-1 composite freshness * popularity
    # v1.6.0-P1.A: human-flagged manual status
    manual_status: str | None = None  # 'no_value' | 'pinned' | null
    manual_status_reason: str | None = None
    manual_status_set_at: datetime | None = None
    manual_status_set_by: str | None = None
    # v1.6.0-P1.C-1: JSON array of query strings that discovered this repo
    discovered_via_queries: list[str] = Field(default_factory=list)
    # v1.6.1 multi-source: list of source entries [{kind,id,stars/likes,url,last_seen_at}, ...]
    # One profile merges multiple sources (GitHub + HF Space + GitLab) for display, instead of building multiple
    # profiles for the same project
    sources: list[dict] = Field(default_factory=list)
    # v1.6.1 primary source — decides canonical URL/title; default 'github' for legacy rows
    primary_source: str = "github"


# ============================================================
# Ecosystem extension models (Stage B)
# ============================================================


class EcosystemDeepReviewStatus(enum.StrEnum):
    """Deep-scan report status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IntegrationRecommendation(enum.StrEnum):
    """Integration recommendation level."""

    INTEGRATE = "integrate"
    REFERENCE = "reference"
    LEARN = "learn"
    SKIP = "skip"


class DemoResult(enum.StrEnum):
    """Demo run result."""

    SUCCESS = "success"
    FAIL = "fail"
    SKIPPED = "skipped"


class EcosystemTagCategory(enum.StrEnum):
    """Ecosystem tag category."""

    CAPABILITY = "capability"
    TECH_STACK = "tech_stack"
    MATURITY = "maturity"
    POSITIONING = "positioning"


class EcosystemTagSource(enum.StrEnum):
    """Tag source.

    v1.5.0-A adds LIFECYCLE — used by funnel Stage 3 to mark reference / integrated /
    deleted / private_now / evaluating, written automatically by the ecosystem lifecycle.
    """

    GITHUB_TOPIC = "github_topic"
    AUTO_RULE = "auto_rule"
    AUTO_LLM = "auto_llm"
    MANUAL = "manual"
    LIFECYCLE = "lifecycle"


class EcosystemStageStatus(enum.StrEnum):
    """Ecosystem repo deep-scan funnel stage status (v1.5.0).

    Progressive accumulation: queued → shallow_done → architecture_done → debated →
    referenced / integrated. Each *_failed sub-state means that stage failed even after 5 retries,
    without affecting other stages' advancement.
    """

    QUEUED = "queued"
    SHALLOW_DONE = "shallow_done"
    SHALLOW_FAILED = "shallow_failed"
    ARCHITECTURE_DONE = "architecture_done"
    ARCHITECTURE_FAILED = "architecture_failed"
    DEBATED = "debated"
    DEBATED_FAILED = "debated_failed"
    REFERENCED = "referenced"
    INTEGRATED = "integrated"


# D5 convergence (2026-07): ``stage_status`` is the single authoritative axis
# for deep-review funnel progress; the legacy ``status`` column is demoted to
# a derived read-only view of it. This mapping is the SINGLE SOURCE OF TRUTH —
# the storage choke points (repository.create_deep_review /
# update_deep_review_stage) and the startup backfill
# (repository.backfill_deep_review_dual_axis) all derive from it.
# Do NOT duplicate this mapping in repository / services / routes / frontend.
STAGE_TO_STATUS: dict[EcosystemStageStatus, EcosystemDeepReviewStatus] = {
    EcosystemStageStatus.QUEUED: EcosystemDeepReviewStatus.QUEUED,
    EcosystemStageStatus.SHALLOW_DONE: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.ARCHITECTURE_DONE: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.DEBATED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.REFERENCED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.INTEGRATED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.SHALLOW_FAILED: EcosystemDeepReviewStatus.FAILED,
    EcosystemStageStatus.ARCHITECTURE_FAILED: EcosystemDeepReviewStatus.FAILED,
    EcosystemStageStatus.DEBATED_FAILED: EcosystemDeepReviewStatus.FAILED,
}


def derive_status_from_stage(
    stage: EcosystemStageStatus | str,
) -> EcosystemDeepReviewStatus:
    """Derive the legacy ``status`` view from the authoritative ``stage_status``.

    Accepts either the enum or its string value (normalized first).
    Raises ``ValueError`` for unknown stage strings — same contract as
    ``EcosystemStageStatus(...)``.
    """
    if isinstance(stage, str):
        stage = EcosystemStageStatus(stage)
    return STAGE_TO_STATUS[stage]


class EcosystemRelationType(enum.StrEnum):
    """Repo-to-repo relation type."""

    INSPIRED_BY = "inspired_by"
    FORKS = "forks"
    EXTENDS = "extends"
    COMPETES = "competes"
    DEPENDS_ON = "depends_on"


class EcosystemScanStrategy(enum.StrEnum):
    """Scan strategy."""

    INCREMENTAL = "incremental"
    FULL = "full"
    TOPIC = "topic"
    TRENDING = "trending"


# ============================================================
# v1.6.0 P0: Multi-source data model types
# ============================================================


class DataSourceKind(enum.StrEnum):
    """Supported ecosystem data source kinds."""

    GITHUB = "github"
    HUGGINGFACE = "huggingface"
    NPM = "npm"
    PYPI = "pypi"
    HACKERNEWS = "hackernews"
    PRODUCTHUNT = "producthunt"
    ARXIV = "arxiv"
    CUSTOM = "custom"


class RepoActiveStatus(enum.StrEnum):
    """Active status of a repo in the ecosystem index."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    STALE = "stale"
    ARCHIVED = "archived"


class EcosystemShallowBatch(BaseModel):
    """Shallow-scan batch — aggregates the meta information and candidate repo snapshot of one batch shallow scan.

    State flow: pending_approval → (approved → running → completed) / cancelled
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    triggered_by: str  # 'cron' / 'manual' / 'user'
    trigger_reason: str | None = None
    candidates_count: int = 0
    candidates_snapshot_json: str | None = None  # JSON list of repo_id
    status: str = "pending_approval"  # pending_approval / approved / running / completed / cancelled
    approved_by: str | None = None
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    new_repos_count: int = 0
    updated_repos_count: int = 0
    metadata_changed_count: int = 0
    failed_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EcosystemDeepReview(BaseModel):
    """Ecosystem repo deep-scan report — a structured analysis of a single repo.

    FK relations: repo_id → EcosystemRepoProfile.id (CASCADE), report_id → Report.id (optional).
    Project isolation: project_id=None means global/unowned; a deep-scan report belongs to the initiating project.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    status: EcosystemDeepReviewStatus = EcosystemDeepReviewStatus.QUEUED
    agent_id: str | None = None  # the agent that executed this deep scan
    summary_md: str = ""
    architecture_md: str = ""
    demo_result: DemoResult | None = None
    demo_log_excerpt: str = ""
    risks_md: str = ""
    learnings_md: str = ""
    integration_recommendation: IntegrationRecommendation | None = None
    report_id: str | None = None  # FK -> Report.id
    dispatch_prompt: str = ""  # sub-agent dispatch prompt (separate from demo_log_excerpt)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)
    # v1.5.0-A extension: progressive funnel stage state machine + associated meeting/integration task
    stage_status: EcosystemStageStatus = EcosystemStageStatus.QUEUED  # funnel stage status
    integration_md: str = ""  # Stage 2 detailed integration suggestion (not just an enum)
    shallow_completed_at: datetime | None = None  # Stage 0 completion time
    architecture_completed_at: datetime | None = None  # Stage 1 completion time
    debated_at: datetime | None = None  # Stage 2 debate end time
    stage3_completed_at: datetime | None = None  # Stage 3 referenced/integrated completion time
    debate_meeting_id: str | None = None  # FK -> Meeting.id (the Stage 2 triggering meeting)
    integration_task_id: str | None = None  # FK -> Task.id (the Stage 3 integrate dispatch task)
    # v1.5.3: worker pool claim fields
    claimed_by: str | None = None  # worker_id string; non-None while claimed
    claimed_at: datetime | None = None  # claim timestamp
    quality_score: int | None = None  # 0-100 review quality score
    quality_notes: str | None = None  # review rationale
    reviewed_by: str | None = None  # the quality reviewer's worker_id
    reviewed_at: datetime | None = None  # quality review completion time
    # v1.7.0: associated shallow-scan batch
    batch_id: str | None = None  # FK -> EcosystemShallowBatch.id


class EcosystemTag(BaseModel):
    """Capability tag dictionary — describes an ecosystem repo's capabilities / tech stack / maturity / positioning."""

    id: str = Field(default_factory=_new_id)
    name: str  # unique, e.g. "memory_system"
    aliases: list[str] = Field(default_factory=list)
    category: EcosystemTagCategory
    description: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemRepoTag(BaseModel):
    """Repo-tag many-to-many association.

    FK relations: repo_id → EcosystemRepoProfile.id (CASCADE), tag_id → EcosystemTag.id (RESTRICT).
    Unique constraint: (repo_id, tag_id).
    Project isolation: project_id follows the project owning repo_id.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    tag_id: str  # FK -> EcosystemTag.id
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: EcosystemTagSource = EcosystemTagSource.MANUAL
    agent_id: str | None = None  # the tagger
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemRelation(BaseModel):
    """Repo-to-repo reference / derivation relation.

    FK relations: from_repo_id / to_repo_id → EcosystemRepoProfile.id (CASCADE).
    Project isolation: research output internal to a project, not shared across projects.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    from_repo_id: str  # FK -> EcosystemRepoProfile.id
    to_repo_id: str  # FK -> EcosystemRepoProfile.id
    relation_type: EcosystemRelationType
    evidence: str = ""  # source note
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemScanRun(BaseModel):
    """Scan batch record — the execution metadata and statistics of one scan task.

    Project isolation: scan history belongs to the project that initiated the scan.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    repos_added: int = 0
    repos_updated: int = 0
    repos_skipped: int = 0
    # v1.6.1 Phase 2: count repos with actual metadata changes (topics/stars/desc/lang)
    metadata_changed_count: int = 0
    errors: list[str] = Field(default_factory=list)
    notes: str = ""
    triggered_by: str = "manual"  # "manual" / "cron"
    agent_id: str | None = None


class EcosystemRepoStatusSnapshot(BaseModel):
    """Per-scan repo state snapshot (v1.5.0-A decision D — append-only, never cleaned up).

    Used to track historical changes such as star rises/falls, push frequency, and entering/leaving the active set.
    After each scan, one row is written for every repo in the active set; users can view the historical timeline in the
    UI.
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    scan_run_id: str  # FK -> EcosystemScanRun.id (the triggering scan batch)
    snapshot_at: datetime = Field(default_factory=utc_now)
    stars: int = 0
    pushed_at: datetime | None = None
    is_archived: bool = False  # GitHub archived state
    is_active: bool = True  # whether it was in the project's active set at the time
    summary_at_time: str = ""  # the shallow_summary at the time (for historical comparison)


class EcosystemProjectSettings(BaseModel):
    """Each project's ecosystem config (v1.5.0-A decision C — project-customized thresholds).

    When a project first accesses the ecosystem, the system automatically creates a default row;
    the AI Team OS project uses stricter defaults (min_stars=5000, top_n=200).
    """

    project_id: str  # primary key — one row per project
    min_stars: int = 1000  # archiving threshold
    top_n: int = 200  # active set size (top N sorted by stars)
    refresh_interval_days: int = 7  # shallow-scan refresh interval
    auto_shallow_on_archive: bool = True  # whether Stage 0 runs automatically on archiving
    focus_topics: list[str] = Field(default_factory=list)  # followed-topic whitelist (empty = all topics)
    focus_languages: list[str] = Field(default_factory=list)  # followed-language whitelist (empty = all languages)
    # Decision F: test-driven adjusted concurrency config
    shallow_concurrency: int = 5
    deep_concurrency: int = 3
    # v1.6.1 Phase 2: migrated from scan_profile.alert_thresholds.max_new_per_scan
    alert_max_new_per_scan: int = 50
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ============================================================
# v1.6.0 P0: Multi-source data model Pydantic types
# ============================================================


class NormalizedSignal(BaseModel):
    """Cross-source normalized activity/popularity signal."""

    popularity_rank: int = 0
    popularity_percentile: float = 0.0  # 0-1, where 0.99 = top 1%
    last_activity_at: datetime | None = None
    activity_score: float = 0.0  # 0-1 composite score


class DataSource(BaseModel):
    """Ecosystem data source configuration (per-project, multi-source)."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    kind: DataSourceKind
    name: str
    config: dict[str, Any] = Field(default_factory=dict)  # queries/filters/rate_limit
    enabled: bool = True
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScanProfile(BaseModel):
    """Ecosystem scan profile — versioned config for active/inactive/archive thresholds."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    version: int = 1
    profile: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemIndexDiff(BaseModel):
    """Record of a single index_update run's diff output (new/reactivated/deactivated/etc.)."""

    id: str = Field(default_factory=_new_id)
    scan_run_id: str | None = None
    project_id: str | None = None
    diff_type: str = "incremental"  # 'initial' | 'incremental'
    new_count: int = 0
    reactivated_count: int = 0
    deactivated_count: int = 0
    stale_count: int = 0
    archived_count: int = 0  # deprecated: kept for backward compat, use github_archived_changed_count
    # v1.6.0-P1 hotfix: new semantically-correct column names
    github_archived_changed_count: int = 0
    removed_from_query_count: int = 0
    details_json: dict[str, Any] = Field(default_factory=dict)
    markdown_summary: str = ""
    alerted: bool = False
    generated_at: datetime = Field(default_factory=utc_now)


class EcosystemStatusChange(BaseModel):
    """Tracks individual repo status transitions (active → inactive, etc.)."""

    id: str = Field(default_factory=_new_id)
    repo_id: str
    project_id: str | None = None
    from_status: str | None = None
    to_status: str
    scan_run_id: str | None = None
    reason: str = ""
    triggered_at: datetime = Field(default_factory=utc_now)


class EcosystemRepoEvent(BaseModel):
    """Full event log for every operation on an ecosystem repo.

    Replaces index_diffs as source-of-truth for change tracking. Diff views
    are computed dynamically by grouping events over a time window.
    """

    id: str = Field(default_factory=_new_id)
    repo_id: str
    project_id: str | None = None
    # 'discovered'|'rescanned'|'topics_changed'|'stars_jumped'|'status_changed'
    # |'archived'|'manual_pinned'|'manual_unpinned'|'removed_from_query'
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    source: str = "scanner"  # 'scanner' | 'manual' | 'api'
    scan_run_id: str | None = None
    # Kept for status_changed compat with EcosystemStatusChange
    from_status: str | None = None
    to_status: str | None = None
    reason: str | None = None
    triggered_at: datetime = Field(default_factory=utc_now)


# ============================================================
# Result types
# ============================================================


class TaskResult(BaseModel):
    """Task execution result."""

    task_id: str
    status: TaskStatus
    result: str
    agent_outputs: dict[str, str] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class TeamStatusSummary(BaseModel):
    """Team status summary."""

    team: Team
    agents: list[Agent]
    active_tasks: list[Task]
    completed_tasks: int = 0
    total_tasks: int = 0
