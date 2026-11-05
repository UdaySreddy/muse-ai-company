"""AI Team OS — Data persistence repository.

StorageRepository is the unified entry point for all database operations.
Upper-layer modules access data only through this interface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import String as SAString
from sqlalchemy import and_, case, delete, func, or_, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from aiteam.api.exceptions import NotFoundError
from aiteam.clock import utc_now
from aiteam.services.usage_coverage import LEADER_ROLE, classify_unattributed
from aiteam.storage.connection import get_session
from aiteam.storage.connection import init_db as _init_db
from aiteam.storage.models import (
    AgentActivityModel,
    AgentModel,
    ChannelMessageModel,
    CrossMessageModel,
    EcosystemDataSourceModel,
    EcosystemDeepReviewModel,
    EcosystemIndexDiffModel,
    EcosystemProjectSettingsModel,
    EcosystemRelationModel,
    EcosystemRepoEventModel,
    EcosystemRepoProfileModel,
    EcosystemRepoStatusSnapshotModel,
    EcosystemRepoTagModel,
    EcosystemScanProfileModel,
    EcosystemScanRunModel,
    EcosystemShallowBatchModel,
    EcosystemStatusChangeModel,
    EcosystemTagModel,
    EventModel,
    KnowledgeLinkModel,
    LeaderBriefingModel,
    MeetingMessageModel,
    MeetingModel,
    MemoryModel,
    PhaseModel,
    ProjectModel,
    ReportModel,
    ScheduledTaskModel,
    TaskMemoModel,
    TaskModel,
    TeamModel,
    WakeSessionModel,
    WorkflowAgentModel,
    WorkflowRunModel,
)
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    STAGE_TO_STATUS,
    Agent,
    AgentActivity,
    AgentStatus,
    AttributionMethod,
    AttributionScope,
    ChannelMessage,
    CrossMessage,
    DataSource,
    DataSourceKind,
    DispatchPopulation,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemIndexDiff,
    EcosystemProjectSettings,
    EcosystemRelation,
    EcosystemRepoEvent,
    EcosystemRepoProfile,
    EcosystemRepoStatusSnapshot,
    EcosystemRepoTag,
    EcosystemScanRun,
    EcosystemShallowBatch,
    EcosystemStageStatus,
    EcosystemStatusChange,
    EcosystemTag,
    EdgeCoverage,
    Event,
    EventType,
    KnowledgeLink,
    LeaderBriefing,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    Memory,
    MemoryScope,
    OrchestrationMode,
    Phase,
    PhaseStatus,
    Project,
    Report,
    ScanProfile,
    ScheduledTask,
    Task,
    TaskMemo,
    TaskStatus,
    Team,
    TokenAttribution,
    TokenMetric,
    TokenSource,
    UnattributedReason,
    UsageCoverageReport,
    UsageCoverageRow,
    WakeSession,
    WorkflowAgent,
    WorkflowRun,
    derive_status_from_stage,
)

# The monotonic advancement order of workflow run statuses (no downgrading on upsert; see upsert_workflow_run).
# killed/failed and completed are terminal states of the same rank: in real data about 10% of runs end as killed/failed
# (without them they would be permanently stuck at running and break the reaper's "no running means zero stat"
# short-circuit);
# same rank allows the legitimate killed→completed transition after resumeFromRunId rewrites the file in place (the
# guard uses >=).
_WF_STATUS_RANK: dict[str, int] = {
    "planned": 0,
    "running": 1,
    "interrupted": 2,
    "completed": 3,
    "killed": 3,
    "failed": 3,
}


# ---------------------------------------------------------------------------
# Data-fetching helpers for token usage attribution (docs/token-attribution-v1-design.md stage 2)
# ---------------------------------------------------------------------------


def _worked_on_agents_subquery(task_id: str = ""):
    """The from_id set of ``agent --worked_on--> task`` edges; when task_id is given, only that task's."""
    stmt = select(KnowledgeLinkModel.from_id).where(
        KnowledgeLinkModel.from_kind == AGENT_TASK_LINK_FROM_KIND,
        KnowledgeLinkModel.to_kind == AGENT_TASK_LINK_TO_KIND,
        KnowledgeLinkModel.link_type == AGENT_TASK_LINK_TYPE,
    )
    if task_id:
        stmt = stmt.where(KnowledgeLinkModel.to_id == task_id)
    return stmt


def _single_worked_on_agents_subquery():
    """Agents that left bookkeeping entries on **one** task only — i.e. those whose four-layer numbers can be cleanly
    attributed to that task.

    Those with entries on two or more cannot enter: the agents five columns are a single total over the whole lifecycle,
    with no per-interval usage
    available to split by edge in time order. §2.4 fixes that in this case they are honestly counted as "task-level
    unattributed", **with no averaging allocation**
    — allocation would fabricate unfalsifiable numbers, which is worse than a gap.
    """
    return (
        _worked_on_agents_subquery()
        .group_by(KnowledgeLinkModel.from_id)
        .having(func.count(func.distinct(KnowledgeLinkModel.to_id)) == 1)
    )


def _apply_created_window(
    stmt: Any,
    since: datetime | None,
    until: datetime | None,
    model: Any = None,
) -> Any:
    """Place the query window on ``created_at``.

    **Not ``tokens_measured_at``** — this is non-negotiable: windowing by measurement time would make unmeasured rows
    vanish straight from the denominator, so coverage would always equal 100% (§4.1 / R2). This is the form of "quietly
    faking the denominator"
    that is easiest to commit and hardest to notice afterwards — the numbers look better, and entirely plausible.
    """
    column = (model or AgentModel).created_at
    if since is not None:
        stmt = stmt.where(column >= since)
    if until is not None:
        stmt = stmt.where(column < until)
    return stmt

# Terminal statuses (rank 3) — a run in any of these is finished. Kept in lockstep
# with workflow_ingest._WF_TERMINAL_STATUSES.
_WF_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "killed", "failed"})

# ------------------------------------------------------------------
# Reserved identities — fake rows must not reach the real ledger
# ------------------------------------------------------------------
# Incident (2026-07-28): tests/unit/test_context_scripts.py runs the real
# pre_compact_save.py hook as a subprocess with session_id="test-session".  The
# hook falls back to localhost:8000 — the *production* instance — whenever the
# port file is missing, which is exactly what an overridden HOME guarantees.  So
# every test-suite run appended a fake checkpoint to the real ledger: 14 of the
# 18 rows in that table were test residue, leaving 4 genuine ones.
#
# The guard sits at the single write entry rather than in the test, because the
# test is only the currently-known caller — any script, smoke run or demo that
# reaches a live API would do the same.  Prefix match (not substring) so ordinary
# ids like "latest-run-7" are untouched.
logger = logging.getLogger(__name__)

RESERVED_ID_PREFIXES: tuple[str, ...] = ("test-", "demo-")

# Whether an agent row carries a token attribution ledger. The criterion is deliberately loose: any one of the four
# usage layers non-zero, **or**
# merely having been measured (``tokens_measured_at`` has a value) — measuring 0 is still a measurement result,
# not the same as "never measured" (no-data != zero). Deleting such a row equals permanently erasing numbers the
# transcript has already
# yielded: the transcript itself gets cleaned up over time, and re-collection is impossible.
_TOKEN_LEDGER_COLUMNS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)


def _carries_token_ledger(agent: Any) -> bool:
    """True when this agent row holds token attribution that cannot be re-measured."""
    if getattr(agent, "tokens_measured_at", None) is not None:
        return True
    return any((getattr(agent, col, None) or 0) > 0 for col in _TOKEN_LEDGER_COLUMNS)


def _holds_unharvested_transcript(agent: Any) -> bool:
    """True when this row is the only pointer to a transcript not yet harvested.

    ``transcript_path`` is recorded only on the agent row: once the row is deleted, even if the transcript still lies on
    disk,
    re-collection can never find it again — the window silently closes; the same failure mode as destroying booked data,
    just one step earlier.
    Rows whose files CC has already cleaned up are not protected: there is nothing to collect, so they fall back to
    ordinary husks.
    When a path cannot be read (permissions etc.), be lenient and treat it as still present — rather keep one extra row
    than wrongly close a window.
    """
    if getattr(agent, "tokens_measured_at", None) is not None:
        return False
    path = getattr(agent, "transcript_path", None)
    if not path:
        return False
    try:
        return Path(path).expanduser().exists()
    except OSError:
        return True

# Payload keys that carry an identity worth guarding.
_IDENTITY_KEYS: tuple[str, ...] = ("session_id", "task_id", "agent_id", "team_id")


def _reserved_identity(data: dict, entity_id: str | None) -> str | None:
    """Return the offending identity when a payload carries a reserved id."""
    candidates: list[str] = [entity_id] if entity_id else []
    if isinstance(data, dict):
        candidates.extend(str(data[k]) for k in _IDENTITY_KEYS if isinstance(data.get(k), str))
    for value in candidates:
        lowered = value.lower()
        if any(lowered.startswith(prefix) for prefix in RESERVED_ID_PREFIXES):
            return value
    return None

# ------------------------------------------------------------------
# Ecosystem claim lease (2026-07-27 queue-deadlock fix)
# ------------------------------------------------------------------
# A ``queued`` deep-review row is *reserved* the moment it is dispatched
# (``claimed_by`` written inside the INSERT — D5) so ``tick`` dispatch and the
# pull-based ``claim_next_shallow_repo`` can never grab the same row. That
# reservation used to be **eternal**: when the dispatched sub-agent died without
# advancing the stage, the row stayed ``queued + claimed`` forever, invisible to
# every claimer (production: 74 rows wedged for 17 days, pending=0).
#
# The reservation is therefore a *lease*: after this TTL it expires and any
# claimer may take the row over. Sized above every in-flight budget so a lease is
# never stolen from a live worker — Stage 0 timeout is 600 s
# (``STAGE0_TIMEOUT_SECONDS``) and the deep-review watchdog defaults to 45 min.
STALE_CLAIM_TTL_SECONDS: int = 3600  # 60 min


def _team_scope_clause(team_ids: list[str]):
    """SQL predicate for "this event belongs to one of these teams".

    Events do NOT carry a team_id column, and — measured on the live 229k-row
    table — **zero** rows put a team id in ``entity_id``. The old predicate
    (``entity_id IN team_ids``) therefore matched nothing at all, which silently
    turned every team/project-scoped event query into an empty list.

    Team membership is expressed four different ways depending on the emitter:
      * team-level events  → ``source = 'team:<team_id>'``
      * agent-level events → ``source = 'agent:<agent_id>'`` (intent stream)
        or ``entity_id = <agent_id>`` with ``source='repository'`` (CRUD stream)
      * task-level events  → ``entity_id = <task_id>``
    All four are unioned here so the caller gets the team's real activity feed.
    """
    from sqlalchemy import literal

    agent_ids = select(AgentModel.id).where(AgentModel.team_id.in_(team_ids))
    task_ids = select(TaskModel.id).where(TaskModel.team_id.in_(team_ids))
    agent_sources = select(literal("agent:") + AgentModel.id).where(
        AgentModel.team_id.in_(team_ids)
    )
    team_sources = [f"team:{tid}" for tid in team_ids]
    return or_(
        EventModel.source.in_(team_sources),
        EventModel.source.in_(agent_sources),
        EventModel.entity_id.in_(team_ids),
        EventModel.entity_id.in_(agent_ids),
        EventModel.entity_id.in_(task_ids),
    )


# update_agent's "pure bookkeeping" field set: when a change involves only these fields, no agent.updated event is
# recorded.
# Only last_active_at is in it — it is a heartbeat timestamp; once the column is written the job is done, and no
# detection, page,
# or API reads it from the event stream. The ctx_* watermark group (6.1% of agent.updated in the last 7 days) is
# **deliberately
# not in this set**: they are explainable state changes (the context grew), and keeping them in the event stream is
# informative.
_SILENT_AGENT_UPDATE_FIELDS = frozenset({"last_active_at"})


class WorkflowRunUpsert(NamedTuple):
    """Result of :meth:`StorageRepository.upsert_workflow_run`.

    ``became_completed`` / ``became_terminal`` report — atomically, within the same
    transaction that read the old status and wrote the new one — whether *this* upsert
    was the call that transitioned the run into ``completed`` / any terminal state. The
    caller uses them for once-only event emission instead of a check-then-act read done
    outside the write transaction (which raced across three un-serialised ingest
    drivers → duplicate ``workflow.completed``; audit WP10).
    """

    run: WorkflowRun
    became_completed: bool
    became_terminal: bool


def _merge_wf_source(old: str | None, new: str | None) -> str:
    """Merge a workflow run's data-plane provenance labels: hook + file → 'hook+file' (order-preserving dedup)."""
    tokens: list[str] = []
    for src in (old or "", new or ""):
        for part in src.split("+"):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return "+".join(tokens) if tokens else (new or old or "")


def _parse_memo_timestamp(raw: object) -> datetime:
    """Parse an old memo's timestamp string into a datetime; fall back to now when unparseable."""
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return utc_now()


def _task_memo_to_legacy(memo: TaskMemo) -> dict[str, Any]:
    """Convert a TaskMemo row into the old config['memo'] array element shape (zero changes to the write interface).

    Keeps the original timestamp/author/content/type keys and additionally attaches the real id for referencing /
    frontend location.
    """
    return {
        "id": memo.id,
        "timestamp": memo.created_at.isoformat() if memo.created_at else "",
        "author": memo.author,
        "content": memo.content,
        "type": memo.memo_type,
    }


class StorageRepository:
    """Data persistence repository — unified data access interface."""

    def __init__(
        self,
        db_url: str | None = None,
        project_scope: str = "",
        unresolved_dir: str = "",
    ) -> None:
        self._db_url = db_url
        self._project_scope = project_scope  # Current project ID; empty = no filtering
        # Unregistered-dir cwd: carried through when X-Project-Dir did not resolve
        # to a project, so direction-memory scoping can bucket by a dir fingerprint
        # instead of silently falling back to the global "system" bucket. NEVER used
        # for project_id table filtering — it is a memory-scope-only concern.
        self._unresolved_dir = unresolved_dir

    def _apply_project_filter(self, query: Any, model_class: Any) -> Any:
        """Auto-apply project_id filter if project_scope is set and model has project_id."""
        if self._project_scope and hasattr(model_class, "project_id"):
            query = query.where(model_class.project_id == self._project_scope)
        return query

    async def init_db(self) -> None:
        """Initialize database (create tables / run migrations)."""
        await _init_db(self._db_url)

    # ================================================================
    # Projects
    # ================================================================

    async def create_project(
        self,
        name: str,
        root_path: str = "",
        description: str = "",
        config: dict | None = None,
    ) -> Project:
        """Create a project."""
        project = Project(
            name=name,
            root_path=root_path,
            description=description,
            config=config or {},
        )
        orm = ProjectModel.from_pydantic(project)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return project

    async def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_projects(self) -> list[Project]:
        """List all projects."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).order_by(ProjectModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_project(self, project_id: str, **kwargs: object) -> Project | None:
        """Update project information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            kwargs["updated_at"] = utc_now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project with full cascade cleanup of all associated data."""
        async with get_session(self._db_url) as session:
            # Check project exists
            proj = await session.get(ProjectModel, project_id)
            if not proj:
                return False

            # Cascade: delete meeting messages for project meetings
            meeting_ids_stmt = select(MeetingModel.id).where(MeetingModel.project_id == project_id)
            await session.execute(
                delete(MeetingMessageModel).where(
                    MeetingMessageModel.meeting_id.in_(meeting_ids_stmt)
                )
            )
            # Cascade: meetings
            await session.execute(delete(MeetingModel).where(MeetingModel.project_id == project_id))
            # Cascade: task memos (deleted before tasks — the task_id subquery fallback needs tasks still present;
            # direct delete by project_id + subquery covering historical rows with empty project_id)
            task_ids_stmt = select(TaskModel.id).where(TaskModel.project_id == project_id)
            await session.execute(
                delete(TaskMemoModel).where(
                    (TaskMemoModel.project_id == project_id)
                    | (TaskMemoModel.task_id.in_(task_ids_stmt))
                )
            )
            # Cascade: tasks (includes subtasks via project_id)
            await session.execute(delete(TaskModel).where(TaskModel.project_id == project_id))
            # The team id must be **materialized before deleting the teams row**: the agents / team memories / events
            # cleanups below
            # all clean up by it; with a lazy subquery, once teams is deleted the subquery is always empty — the events
            # cleanup
            # would have been cleaning nothing all along (reproduced in practice, batch 3, 2026-07-27).
            team_ids = list(
                (
                    await session.execute(
                        select(TeamModel.id).where(TeamModel.project_id == project_id)
                    )
                ).scalars()
            )
            # Cascade: agents for project teams
            if team_ids:
                await session.execute(
                    delete(AgentModel).where(AgentModel.team_id.in_(team_ids))
                )
            # Cascade: teams
            await session.execute(delete(TeamModel).where(TeamModel.project_id == project_id))
            # Cascade: phases
            await session.execute(delete(PhaseModel).where(PhaseModel.project_id == project_id))
            # Cascade: reports
            await session.execute(delete(ReportModel).where(ReportModel.project_id == project_id))
            # Cascade: briefings
            await session.execute(delete(LeaderBriefingModel).where(LeaderBriefingModel.project_id == project_id))
            # Cascade: memories (project-scoped)
            await session.execute(
                delete(MemoryModel).where(
                    (MemoryModel.scope == "project") & (MemoryModel.scope_id == project_id)
                )
            )
            # Cascade: memories (team-scoped) — team knowledge with scope='team' + scope_id=<team_id>.
            # Missing them in deletion is exactly the direct cause of orphan memories: once the teams row is gone, these
            # entries' scope_id points at a non-
            # existent team, and from then on they can neither be queried nor deleted (historical orphans are a separate
            # topic; here we only plug new ones).
            if team_ids:
                await session.execute(
                    delete(MemoryModel).where(
                        (MemoryModel.scope == "team") & (MemoryModel.scope_id.in_(team_ids))
                    )
                )
            # Cascade: cross-project messages
            await session.execute(
                delete(CrossMessageModel).where(
                    (CrossMessageModel.from_project_id == project_id)
                    | (CrossMessageModel.to_project_id == project_id)
                )
            )
            # Cascade: events for project teams (the events table has no team_id column —
            # events associate with teams by entity_id, see the same usage in list_events; this spot once wrote
            # EventModel.team_id, making project_delete constantly 500, measured 2026-07-08)
            if team_ids:
                await session.execute(
                    delete(EventModel).where(EventModel.entity_id.in_(team_ids))
                )
            # Finally: delete project itself
            await session.execute(delete(ProjectModel).where(ProjectModel.id == project_id))
            return True

    async def get_project_by_root_path(self, root_path: str) -> Project | None:
        """Get a project by root_path."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.root_path == root_path)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    # ================================================================
    # Phases
    # ================================================================

    async def create_phase(
        self,
        project_id: str,
        name: str,
        description: str = "",
        order: int = 0,
        config: dict | None = None,
    ) -> Phase:
        """Create a phase."""
        phase = Phase(
            project_id=project_id,
            name=name,
            description=description,
            order=order,
            config=config or {},
        )
        orm = PhaseModel.from_pydantic(phase)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return phase

    async def get_phase(self, phase_id: str) -> Phase | None:
        """Get a phase by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(PhaseModel).where(PhaseModel.id == phase_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_phases(self, project_id: str) -> list[Phase]:
        """List all phases under a project, sorted by order."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel)
                .where(PhaseModel.project_id == project_id)
                .order_by(PhaseModel.order, PhaseModel.created_at)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_phase(self, phase_id: str, **kwargs: object) -> Phase | None:
        """Update phase information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(PhaseModel).where(PhaseModel.id == phase_id))
            row = result.scalar_one_or_none()
            if row is None:
                return None

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, PhaseStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    PhaseStatus(status_val)

            kwargs["updated_at"] = utc_now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_phase(self, phase_id: str) -> bool:
        """Delete a phase."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(PhaseModel).where(PhaseModel.id == phase_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    async def get_active_phase(self, project_id: str) -> Phase | None:
        """Get the currently active phase of a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(
                    PhaseModel.project_id == project_id,
                    PhaseModel.status == PhaseStatus.ACTIVE.value,
                )
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def deactivate_phases(self, project_id: str) -> int:
        """Set all active phases under a project to completed."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(
                    PhaseModel.project_id == project_id,
                    PhaseModel.status == PhaseStatus.ACTIVE.value,
                )
            )
            rows = result.scalars().all()
            for row in rows:
                row.status = PhaseStatus.COMPLETED.value
                row.updated_at = utc_now()
            return len(rows)

    # ================================================================
    # Teams
    # ================================================================

    async def create_team(
        self,
        name: str,
        mode: str,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Team:
        """Create a team."""
        # Auto-fill project_id from scope if not explicitly provided
        effective_project_id = kwargs.get("project_id") or (self._project_scope or None)
        team = Team(
            name=name,
            mode=OrchestrationMode(mode),
            config=config or {},
            project_id=effective_project_id,
            leader_agent_id=kwargs.get("leader_agent_id"),
        )
        orm = TeamModel.from_pydantic(team)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return team

    async def get_or_create_team(
        self,
        name: str,
        mode: str,
        config: dict | None = None,
        **kwargs: Any,
    ) -> tuple[Team, bool]:
        """Race-safe find-or-create by unique team name. Returns (team, created).

        Concurrent hook events can both miss ``get_team_by_name`` and then race the
        INSERT; ``teams.name`` UNIQUE makes the loser raise IntegrityError (an
        uncaught 500 that drops the hook event — 2026-07-21 session-e713a6cb).
        Symmetric to the agents ``cc_tool_use_id`` B1 fix (4f508bd): swallow the
        conflict and re-fetch the winner's row. Callers gate post-create side
        effects (event emit / project link / member migration) on ``created``.
        """
        existing = await self.get_team_by_name(name)
        if existing is not None:
            return existing, False
        try:
            team = await self.create_team(name, mode, config=config, **kwargs)
            return team, True
        except IntegrityError:
            # Lost the create race — the winner's row is now committed; re-fetch it.
            # A non-name integrity error would re-fetch None, so re-raise then.
            existing = await self.get_team_by_name(name)
            if existing is None:
                raise
            return existing, False

    async def get_team(self, team_id: str) -> Team | None:
        """Get a team by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.id == team_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def get_team_by_name(self, name: str) -> Team | None:
        """Get a team by name."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.name == name))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_teams(self) -> list[Team]:
        """List all teams."""
        async with get_session(self._db_url) as session:
            stmt = select(TeamModel).order_by(TeamModel.created_at.desc())
            stmt = self._apply_project_filter(stmt, TeamModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_teams_by_project(self, project_id: str) -> list[Team]:
        """List all teams under a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.project_id == project_id)
                .order_by(TeamModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def repair_team_project_attribution(
        self,
        *,
        dry_run: bool = True,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """List (or fix) teams whose project ownership contradicts the "in-team Leader row", together with their
        members.

        The cleanup targets the wrong rows left before 2026-07-27 by ``deps._auto_create_projects`` bulk-claiming orphan
        teams using the **API process's
        cwd**: a team got recorded as the project of the server's startup directory, while the in-team
        Leader row (locked at session start by that session's cwd) points at another project. Agent rows were inherited
        from the team by
        ``_on_subagent_start``, so they are wrong along with it.

        The criterion is conservative — only when **the same-team Leader row has an explicit project_id that disagrees
        with the team** does it count as a wrong
        ledger entry. No Leader, a Leader unbound to a project, or kind=workflow (ownership backfilled by the
        observation layer from the landing slug)
        are all left untouched: rather leave them ownerless than guess once more.

        Returns a row per affected team with ``agents_fixed`` counting the member
        rows realigned alongside it. Writes nothing when ``dry_run`` is True.
        """
        findings: list[dict[str, Any]] = []
        async with get_session(self._db_url) as session:
            teams = (await session.execute(select(TeamModel).limit(limit))).scalars().all()
            for team in teams:
                if (team.config or {}).get("kind") == "workflow":
                    continue
                members = (
                    (
                        await session.execute(
                            select(AgentModel).where(AgentModel.team_id == team.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                authoritative = next(
                    (a.project_id for a in members if a.role == "leader" and a.project_id),
                    None,
                )
                if not authoritative or authoritative == team.project_id:
                    continue
                stale_members = [
                    a for a in members if a.role != "leader" and a.project_id != authoritative
                ]
                findings.append(
                    {
                        "team_id": team.id,
                        "team_name": team.name,
                        "from_project_id": team.project_id,
                        "to_project_id": authoritative,
                        "agents_fixed": len(stale_members),
                    }
                )
                if not dry_run:
                    team.project_id = authoritative
                    team.updated_at = utc_now()
                    for a in stale_members:
                        a.project_id = authoritative
        return findings

    async def find_active_team_by_leader(self, leader_agent_id: str) -> Team | None:
        """Find the active team currently led by a Leader."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.leader_agent_id == leader_agent_id)
                .where(TeamModel.status == "active")
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_leader_by_project(self, project_id: str) -> Agent | None:
        """Find the Leader agent for a project (role=leader + project_id match)."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(AgentModel)
                .where(AgentModel.project_id == project_id)
                .where(AgentModel.role == "leader")
                .order_by(AgentModel.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_team(self, team_id: str, **kwargs: object) -> Team:
        """Update team information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.id == team_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Team {team_id} does not exist"
                raise NotFoundError(msg)

            # Handle mode field: convert to string value
            if "mode" in kwargs:
                mode_val = kwargs["mode"]
                if isinstance(mode_val, OrchestrationMode):
                    kwargs["mode"] = mode_val.value
                elif isinstance(mode_val, str):
                    # Validate the value is valid
                    OrchestrationMode(mode_val)

            kwargs["updated_at"] = utc_now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_team(self, team_id: str) -> bool:
        """Delete a team."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(TeamModel).where(TeamModel.id == team_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    # A container team's list of "substantive records": carrying any one of them means the team is not an empty shell
    # and is never cleaned up.
    # Every item is something the user might still go look at; rather let a few extra empty shells remain than delete a
    # single record.
    _CONTAINER_KEEP_IF_ANY: tuple[tuple[Any, str], ...] = (
        (TaskModel, "team_id"),
        (MeetingModel, "team_id"),
        (ReportModel, "team_id"),
        (ScheduledTaskModel, "team_id"),
        (WakeSessionModel, "team_id"),
        (WorkflowRunModel, "team_id"),
    )

    async def purge_stale_session_containers(
        self,
        *,
        retention_days: int = 7,
        limit: int = 20,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Delete spent ``session-<sid8>`` container teams and their roster.

        A CC session owns exactly one container team and never reuses another's
        (fleet layer section 3), so a long-running CC process leaves one completed
        husk behind per session it starts. They are not an accounting error — the
        user ruled (2026-07-27) that one team per session is the correct model and
        that the husks should simply be swept up periodically.

        Deletion is deliberately hard to earn. A team is purged only when it is a
        container by BOTH markers (``config.kind == "session"`` and the
        ``session-`` name), is already ``completed``, has aged past
        ``retention_days``, has no busy or recently active member, and carries none
        of ``_CONTAINER_KEEP_IF_ANY``. Age comes from ``completed_at`` when present
        and falls back to ``updated_at`` / ``created_at`` — the close paths do not
        all stamp ``completed_at`` (13 of 13 purge candidates had it NULL in the
        2026-07-27 production reading).

        The roster goes with the team: members and their activity rows are removed
        in the same transaction, since neither means anything once the team is gone
        and this repository has no FK cascades (see ``delete_project``).

        ``retention_days <= 0`` disables the purge outright. Returns one summary
        row per purged team; with ``dry_run`` it returns the same rows and writes
        nothing.
        """
        if retention_days <= 0 or limit <= 0:
            return []

        now = utc_now()
        cutoff = now - timedelta(days=retention_days)
        age_basis = func.coalesce(
            TeamModel.completed_at, TeamModel.updated_at, TeamModel.created_at
        )
        purged: list[dict[str, Any]] = []

        async with get_session(self._db_url) as session:
            candidates = (
                (
                    await session.execute(
                        select(TeamModel)
                        .where(
                            TeamModel.status == "completed",
                            TeamModel.name.like("session-%"),
                            age_basis < cutoff,
                        )
                        .order_by(age_basis)
                    )
                )
                .scalars()
                .all()
            )

            for team in candidates:
                if (team.config or {}).get("kind") != "session":
                    continue

                members = (
                    (
                        await session.execute(
                            select(AgentModel).where(AgentModel.team_id == team.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                # A member still busy / recently active = this team is still in use — harder evidence than any timestamp
                # on the team.
                if any(m.status == "busy" for m in members):
                    continue
                if any(m.last_active_at and m.last_active_at >= cutoff for m in members):
                    continue
                # A member row carrying a usage ledger, or still pointing at a transcript that has not been collected
                # yet and is still on disk
                # = never clean up. This gate's criterion differs from the six tables above: those ask "does this team
                # carry records someone will still go
                # look at"; this one asks "would deleting these rows destroy data that can never be collected again".
                # The token
                # five columns are parsed from transcripts, and the path is recorded only on the row itself — once the
                # row is gone, the ledger and
                # the re-collection window vanish permanently together, unlike tasks/reports, which can be rebuilt from
                # elsewhere.
                # Timing is the root cause of this gap: the purge shipped with v1.11.0 (2026-07-27), while the token
                # five columns
                # only landed on agent rows in v1.11.1 (2026-07-29), and the cleanup gate never went back to add this
                # rule.
                if any(
                    _carries_token_ledger(m) or _holds_unharvested_transcript(m)
                    for m in members
                ):
                    continue

                has_records = False
                for model, column in self._CONTAINER_KEEP_IF_ANY:
                    count = (
                        await session.execute(
                            select(func.count())
                            .select_from(model)
                            .where(getattr(model, column) == team.id)
                        )
                    ).scalar_one()
                    if count:
                        has_records = True
                        break
                if has_records:
                    continue

                member_ids = [m.id for m in members]
                activities = 0
                if member_ids:
                    activities = (
                        await session.execute(
                            select(func.count())
                            .select_from(AgentActivityModel)
                            .where(AgentActivityModel.agent_id.in_(member_ids))
                        )
                    ).scalar_one()

                purged.append(
                    {
                        "team_id": team.id,
                        "name": team.name,
                        "project_id": team.project_id,
                        "agents_deleted": len(member_ids),
                        "activities_deleted": activities,
                        "age_days": round(
                            (
                                now
                                - (team.completed_at or team.updated_at or team.created_at)
                            ).total_seconds()
                            / 86400,
                            1,
                        ),
                    }
                )

                if not dry_run:
                    if member_ids:
                        await session.execute(
                            delete(AgentActivityModel).where(
                                AgentActivityModel.agent_id.in_(member_ids)
                            )
                        )
                        await session.execute(
                            delete(AgentModel).where(AgentModel.team_id == team.id)
                        )
                    await session.execute(delete(TeamModel).where(TeamModel.id == team.id))

                if len(purged) >= limit:
                    break

        return purged

    # ================================================================
    # Agents
    # ================================================================

    async def create_agent(self, team_id: str, name: str, role: str, **kwargs: object) -> Agent:
        """Create an Agent."""
        agent = Agent(
            team_id=team_id,
            name=name,
            role=role,
            system_prompt=str(kwargs.get("system_prompt", "")),
            # Do not persist a specific model by default: when the model is unknown, leave it empty (displayed as --);
            # the real value is backfilled by transcript
            # tail-reads (Leader) / wf terminal state (workflow agents). A default of 'claude-opus-4-7' once
            # kept surfacing at multiple row-creation points and misled the display (root-caused by the user across
            # three practical tests, 2026-07-07).
            model=str(kwargs.get("model", "")),
            config=kwargs.get("config", {}),  # type: ignore[arg-type]
            source=str(kwargs.get("source", "api")),
            session_id=kwargs.get("session_id"),  # type: ignore[arg-type]
            cc_tool_use_id=kwargs.get("cc_tool_use_id"),  # type: ignore[arg-type]
        )
        orm = AgentModel.from_pydantic(agent)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return agent

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get an Agent by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(AgentModel).where(AgentModel.id == agent_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_agents(self, team_id: str) -> list[Agent]:
        """List all Agents in a team."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.team_id == team_id)
                .order_by(AgentModel.created_at)
            )
            stmt = self._apply_project_filter(stmt, AgentModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_agent(self, agent_id: str, **kwargs: object) -> Agent:
        """Update Agent information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(AgentModel).where(AgentModel.id == agent_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Agent {agent_id} not found"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, AgentStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    AgentStatus(status_val)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            updated = row.to_pydantic()

        # Pure heartbeats record no event (Q2, decided by the Creator 2026-07-27, with the hard condition "must not
        # affect any detection function").
        # Heartbeat **detection** is untouched: the criterion has always been the agents.last_active_at column, already
        # written above;
        # what stopped is only the "also write one more event row on the side".
        # Evidence: agent.updated totals 112,349 rows = 47.4% of the events table's 237,126 rows, of which
        # 94,029 rows have changes of exactly last_active_at alone — that one class alone is
        # 39.7% of the whole table (45,717 / 118,867 = 38.5% in the last 7 days).
        # The consumption surface was checked one by one: **zero detection consumers** — StateReaper / wake_actionable /
        # wake_manager / watchdog — none of them read the events table; state_snapshot is written everywhere in the repo
        # but read nowhere;
        # this path goes through repository.create_event (persist only), not the EventBus, so it also
        # never participated in WS real-time refresh — UI refresh has always been driven by cc.tool_use /
        # cc.tool_complete,
        # unrelated to this change. The real consumers are only the /api/events list and team_briefing's
        # recent events, and both improve with this layer of noise gone.
        # Historical rows are all kept per the red line — stop writing, never delete.
        if set(kwargs) <= _SILENT_AGENT_UPDATE_FIELDS:
            return updated

        # Auto-emit snapshot event after state change
        snapshot = {
            "status": updated.status.value if hasattr(updated.status, "value") else str(updated.status),
            "name": updated.name,
        }
        await self.create_event(
            event_type="agent.updated",
            source="repository",
            data={"agent_id": agent_id, "changes": list(kwargs.keys())},
            entity_id=agent_id,
            entity_type="agent",
            state_snapshot=snapshot,
        )
        return updated

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an Agent."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(AgentModel).where(AgentModel.id == agent_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Tasks
    # ================================================================

    async def create_task(
        self, team_id: str | None, title: str, description: str = "", **kwargs: object
    ) -> Task:
        """Create a task.

        team_id can be None (project-level task, not bound to a team).
        """
        # Build optional parameters
        optional: dict[str, object] = {}
        for key in (
            "assigned_to",
            "parent_id",
            "project_id",
            "depends_on",
            "depth",
            "order",
            "template_id",
            "priority",
            "horizon",
            "tags",
            "config",
            "status",
            "cc_task_id",
            "completed_at",
        ):
            if key in kwargs:
                optional[key] = kwargs[key]
        # Set default values
        optional.setdefault("depends_on", [])
        optional.setdefault("depth", 0)
        optional.setdefault("order", 0)

        # Project ownership (fixed 2026-07-27): explicit parameter > the owning team's project_id > request scope.
        #
        # The team binding ranks before the scope because it is a structural fact (the task grows on that team's wall),
        # whereas
        # _project_scope is only this request's view context (the X-Project-Id/Dir headers).
        #
        # Before this tier was added, the four "create a task via a team" entry points (tasks/run, task_decompose's
        # parent/child tasks, issue reporting, TeamManager.execute_task) all failed to write project_id,
        # and MCP-side calls carry no scope header → tasks uniformly got project_id=None, so the project-level task wall
        # (task_list_project / GET /api/projects/{id}/task-wall) could never see them.
        # Fixed at this single choke point rather than the four call sites: new entry points cannot leak it again.
        if not optional.get("project_id") and team_id:
            team = await self.get_team(team_id)
            if team is not None and team.project_id:
                optional["project_id"] = team.project_id
        if self._project_scope and not optional.get("project_id"):
            optional["project_id"] = self._project_scope

        task = Task(
            team_id=team_id,
            title=title,
            description=description,
            **optional,  # type: ignore[arg-type]
        )
        orm = TaskModel.from_pydantic(task)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return task

    async def list_subtasks(self, parent_id: str) -> list[Task]:
        """List all subtasks of a task, sorted by order."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(TaskModel)
                .where(TaskModel.parent_id == parent_id)
                .order_by(TaskModel.order, TaskModel.created_at)
            )
            stmt = self._apply_project_filter(stmt, TaskModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            tasks = [r.to_pydantic() for r in rows]
            await self._hydrate_task_memos(session, tasks)
            return tasks

    async def find_tasks_by_cc_ids(self, cc_task_ids: list[str]) -> dict[str, Task]:
        """Map CC task ids to the OS rows that mirror them (missing ids simply absent).

        Used by the CC task bridge for two things: idempotency (a repeated
        TaskCompleted must not create a second row) and dependency resolution
        (a CC ``blockedBy`` id only becomes an OS ``depends_on`` entry once the
        blocking task has itself been mirrored).
        """
        wanted = [i for i in cc_task_ids if i]
        if not wanted:
            return {}
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.cc_task_id.in_(wanted))
            stmt = self._apply_project_filter(stmt, TaskModel)
            rows = (await session.execute(stmt)).scalars().all()
            return {r.cc_task_id: r.to_pydantic() for r in rows if r.cc_task_id}

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            row = result.scalar_one_or_none()
            if row is None:
                return None
            task = row.to_pydantic()
            await self._hydrate_task_memos(session, [task])
            return task

    async def list_tasks(self, team_id: str, status: TaskStatus | None = None) -> list[Task]:
        """List team tasks, optionally filtered by status."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.team_id == team_id)
            stmt = self._apply_project_filter(stmt, TaskModel)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            tasks = [r.to_pydantic() for r in rows]
            await self._hydrate_task_memos(session, tasks)
            return tasks

    async def list_tasks_by_project(
        self, project_id: str, status: TaskStatus | None = None
    ) -> list[Task]:
        """List all tasks under a project (including project-level tasks with team_id=None and team tasks)."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.project_id == project_id)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_task(self, task_id: str, **kwargs: object) -> Task:
        """Update task information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Task {task_id} not found"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, TaskStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    TaskStatus(status_val)

            # Capture the pre-change status *before* setattr — a failure event
            # without the state it fell from is not attributable after the fact.
            prev_status = str(row.status)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            updated = row.to_pydantic()

        # Auto-emit snapshot event after state change
        snapshot = {
            "status": updated.status.value if hasattr(updated.status, "value") else str(updated.status),
            "assigned_to": updated.assigned_to,
            "title": updated.title,
        }
        await self.create_event(
            event_type="task.updated",
            source="repository",
            data={"task_id": task_id, "changes": list(kwargs.keys())},
            entity_id=task_id,
            entity_type="task",
            state_snapshot=snapshot,
        )

        # Failure observability is bolted onto the state machine, not onto the
        # caller.  Every path that fails a task — API, MCP tool, reaper reclaim,
        # orchestrator — funnels through update_task, so emitting here is the
        # only way to stop depending on an agent voluntarily reporting its own
        # death.  Fires on the *transition* into failed so a retry that fails
        # again is recorded as a new fact, while re-writing the same status is
        # not (the event stream is a ledger, not a log).
        if snapshot["status"] == TaskStatus.FAILED.value and prev_status != TaskStatus.FAILED.value:
            await self.create_event(
                event_type=EventType.TASK_FAILED.value,
                source="repository",
                data={
                    "task_id": task_id,
                    "from_status": prev_status,
                    "to_status": TaskStatus.FAILED.value,
                    "failure_context": updated.result or "",
                    "team_id": updated.team_id,
                    "project_id": updated.project_id,
                    "assigned_to": updated.assigned_to,
                    "title": updated.title,
                    # Distinguishes state-machine capture from a caller's own
                    # report, so later analysis can tell observed from claimed.
                    "detected_by": "state_machine",
                },
                entity_id=task_id,
                entity_type="task",
                state_snapshot=snapshot,
            )
        return updated

    async def get_downstream_tasks(self, task_id: str) -> list[Task]:
        """Find all tasks whose depends_on contains task_id (i.e., downstream tasks depending on this task)."""
        async with get_session(self._db_url) as session:
            # SQL LIKE pre-filter to narrow down results, then Python-level precise check on JSON array
            stmt = select(TaskModel).where(
                TaskModel.depends_on.cast(SAString).contains(task_id),
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            downstream = []
            for row in rows:
                deps = row.depends_on if isinstance(row.depends_on, list) else []
                if task_id in deps:
                    downstream.append(row.to_pydantic())
            return downstream

    async def resolve_task_dependencies(self, task_id: str) -> list[Task]:
        """Cascade unlock when a task completes.

        Check all downstream BLOCKED tasks that depend on this task_id.
        If all their dependencies are completed, unlock them to PENDING.

        Returns:
            List of unblocked tasks.
        """
        downstream = await self.get_downstream_tasks(task_id)
        blocked = [t for t in downstream if t.status == TaskStatus.BLOCKED]
        if not blocked:
            return []

        # Collect all dep IDs we need to check in one batch query
        all_dep_ids: set[str] = set()
        for task in blocked:
            all_dep_ids.update(task.depends_on)

        # Batch-load all dependency tasks with a single IN query
        dep_status: dict[str, str] = {}
        if all_dep_ids:
            async with get_session(self._db_url) as session:
                stmt = select(TaskModel).where(TaskModel.id.in_(all_dep_ids))
                result = await session.execute(stmt)
                for row in result.scalars().all():
                    dep_status[row.id] = row.status

        unblocked: list[Task] = []
        for task in blocked:
            all_deps_done = all(
                dep_status.get(dep_id) == TaskStatus.COMPLETED
                for dep_id in task.depends_on
            )
            if all_deps_done:
                updated = await self.update_task(task.id, status=TaskStatus.PENDING.value)
                unblocked.append(updated)

        return unblocked

    async def detect_dependency_cycle(self, task_id: str, new_dep_id: str) -> bool:
        """Detect if adding a dependency would create a cycle.

        Starting from new_dep_id, trace upstream along the depends_on chain.
        If it leads back to task_id, a cycle would be formed.

        Uses BFS with batch IN queries to avoid N+1 per traversal level.

        Returns:
            True if a cycle exists, False if safe.
        """
        # Self-dependency is a direct cycle
        if task_id == new_dep_id:
            return True

        visited: set[str] = set()
        frontier: set[str] = {new_dep_id}

        while frontier:
            to_fetch = frontier - visited
            if not to_fetch:
                break
            visited.update(to_fetch)

            # Batch-load all frontier nodes in a single IN query
            async with get_session(self._db_url) as session:
                stmt = select(TaskModel).where(TaskModel.id.in_(to_fetch))
                result = await session.execute(stmt)
                rows = result.scalars().all()

            next_frontier: set[str] = set()
            for row in rows:
                deps = row.depends_on if isinstance(row.depends_on, list) else []
                for dep_id in deps:
                    if dep_id == task_id:
                        return True  # Cycle found
                    if dep_id not in visited:
                        next_frontier.add(dep_id)

            frontier = next_frontier

        return False

    # ================================================================
    # Events
    # ================================================================

    async def create_event(
        self,
        event_type: str,
        source: str,
        data: dict,
        entity_id: str | None = None,
        entity_type: str | None = None,
        state_snapshot: dict | None = None,
    ) -> Event:
        """Create a system event.

        Reserved identities (see :data:`RESERVED_ID_PREFIXES`) are refused here —
        this is the single write entry for the events ledger, so the guard cannot
        be routed around.

        Args:
            event_type: Event type string (e.g. "task.created").
            source: Event source identifier.
            data: Full event payload.
            entity_id: ID of the primary entity involved (task/agent/team/meeting).
            entity_type: Entity type label: "task" / "agent" / "team" / "meeting".
            state_snapshot: Trimmed key fields of entity state at event time.
        """
        reserved = _reserved_identity(data, entity_id)
        if reserved:
            # Dropped, not raised: the ledger refusing a fake row must never take
            # down the caller that produced it (hooks are fire-and-forget, and a
            # test-suite run is exactly when this fires).
            logger.warning(
                "events write rejected by guardrail: reserved identity %r (type=%s "
                "source=%s)", reserved, event_type, source
            )
            return Event(
                type=EventType(event_type),
                source=source,
                data=data,
                entity_id=entity_id,
                entity_type=entity_type,
                state_snapshot=state_snapshot,
            )

        event = Event(
            type=EventType(event_type),
            source=source,
            data=data,
            entity_id=entity_id,
            entity_type=entity_type,
            state_snapshot=state_snapshot,
        )
        orm = EventModel.from_pydantic(event)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return event

    async def list_events(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 50,
        type_prefix: str | None = None,
        entity_id: str | None = None,
        team_ids: list[str] | None = None,
    ) -> list[Event]:
        """List events, optionally filtered by type, source, entity_id, or team scope.

        Args:
            event_type: Exact match on event type (e.g., "agent.created")
            source: Exact match on event source
            limit: Maximum number of results to return
            type_prefix: Prefix match on event type (e.g., "decision." matches all decision events)
            entity_id: Filter by entity ID — returns all events for a specific entity
            team_ids: Restrict to events belonging to one of these teams — see
                :func:`_team_scope_clause` for what "belonging" means in practice
        """
        async with get_session(self._db_url) as session:
            stmt = select(EventModel)
            if event_type is not None:
                stmt = stmt.where(EventModel.type == event_type)
            elif type_prefix is not None:
                stmt = stmt.where(EventModel.type.like(f"{type_prefix}%"))
            if source is not None:
                stmt = stmt.where(EventModel.source == source)
            if entity_id is not None:
                stmt = stmt.where(EventModel.entity_id == entity_id)
            if team_ids is not None:
                if not team_ids:
                    # Scope resolved to zero teams — nothing can belong to it.
                    return []
                stmt = stmt.where(_team_scope_clause(team_ids))
            stmt = stmt.order_by(EventModel.timestamp.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def alwaysload_tool_frequencies(
        self,
        *,
        days: int = 7,
        name_prefix: str = "mcp__ai-team-os%",
        min_distinct_days: int = 2,
    ) -> list[tuple[str, int, int]]:
        """Frequency aggregation of recent high-frequency MCP tools (the sole data source for the tool-loading P1
        alwaysLoad rotation).

        One SQL statement: tools in agent_activities from the last ``days`` days matching ``name_prefix``,
        grouped by tool_name, with distinct days (COUNT DISTINCT date(timestamp)) ≥ ``min_distinct_days``
        to fend off time-window bursts, returned in descending total-frequency order.

        Returns:
            [(tool_name, count, distinct_days), ...], frequency descending. tool_name is the raw full name stored in the
            DB
            (including the ``mcp__ai-team-os__`` prefix); normalization happens in the service layer.
        """
        cutoff = utc_now() - timedelta(days=days)
        distinct_days = func.count(func.distinct(func.date(AgentActivityModel.timestamp)))
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentActivityModel.tool_name,
                    func.count().label("c"),
                    distinct_days.label("d"),
                )
                .where(AgentActivityModel.timestamp >= cutoff)
                .where(AgentActivityModel.tool_name.like(name_prefix))
                .group_by(AgentActivityModel.tool_name)
                .having(distinct_days >= min_distinct_days)
                .order_by(func.count().desc())
            )
            result = await session.execute(stmt)
            return [(row[0], int(row[1]), int(row[2])) for row in result.all()]

    # ================================================================
    # Memories
    # ================================================================

    # Direction-layer native kinds (memory system v2 P1): injection/red lines apply only to these four kinds; the legacy
    # team/agent partitions are unaffected.
    DIRECTION_KINDS = ("constraint", "design", "directive", "preference")
    # Injection truncation priority (high→low): constraints/guardrails are kept first; format preferences are cut first.
    DIRECTION_KIND_PRIORITY = {"constraint": 0, "design": 1, "directive": 2, "preference": 3}

    async def create_memory(
        self,
        scope: str,
        scope_id: str,
        content: str,
        metadata: dict | None = None,
        *,
        kind: str = "preference",
        source_refs: list[str] | None = None,
        supersedes: str | None = None,
    ) -> Memory:
        """Create a memory (direction-layer write, zero LLM).

        When supersedes is given, set the superseded row's invalid_at=now and invalidated_by=the new id
        (Zep invalidation semantics: a changed preference = the new entry replaces the old one, no deletion). The volume
        red line is enforced in the service layer
        (POST /api/memories); this place only persists and replaces.
        """
        memory = Memory(
            scope=MemoryScope(scope),
            scope_id=scope_id,
            content=content,
            kind=kind,
            metadata=metadata or {},
            source_refs=source_refs or [],
        )
        async with get_session(self._db_url) as session:
            session.add(MemoryModel.from_pydantic(memory))
            if supersedes:
                res = await session.execute(
                    select(MemoryModel).where(MemoryModel.id == supersedes)
                )
                old = res.scalar_one_or_none()
                if old is not None and old.invalid_at is None:
                    old.invalid_at = memory.created_at
                    old.invalidated_by = memory.id
        return memory

    async def invalidate_memory(
        self, memory_id: str, invalidated_by: str | None = None
    ) -> Memory | None:
        """Explicitly invalidate one direction-layer memory (no deletion, Zep invalidation semantics). Returns the entry
        after invalidation."""
        async with get_session(self._db_url) as session:
            res = await session.execute(
                select(MemoryModel).where(MemoryModel.id == memory_id)
            )
            row = res.scalar_one_or_none()
            if row is None:
                return None
            if row.invalid_at is None:
                row.invalid_at = utc_now()
                row.invalidated_by = invalidated_by
            return row.to_pydantic()

    # count_valid_memories was retired together with the count axis (memory v2.1, 2026-07-31): the volume red line is
    # now checked against
    # per-bucket character quotas, and the over-limit protocol must hand back the full text of all bucket entries,
    # reading list_memories rather than a count.

    async def list_direction_memories(
        self,
        project_id: str | None = None,
        kind: str | None = None,
        include_invalidated: bool = False,
    ) -> list[Memory]:
        """List valid direction-layer entries for the dual hooks' resident injection (the "full direction layer" of
        Zep's dual read).

        Direction layer = scope∈{global, user} (global/user level always included) + scope=project with
        scope_id==project_id (only the current project). By default returns only valid entries, ordered by kind priority
        +
        time descending, so injection can truncate by budget from high priority downward.
        """
        async with get_session(self._db_url) as session:
            scope_clause = MemoryModel.scope.in_(
                [MemoryScope.GLOBAL.value, MemoryScope.USER.value]
            )
            if project_id:
                scope_clause = scope_clause | (
                    (MemoryModel.scope == MemoryScope.PROJECT.value)
                    & (MemoryModel.scope_id == project_id)
                )
            stmt = select(MemoryModel).where(scope_clause)
            if not include_invalidated:
                stmt = stmt.where(MemoryModel.invalid_at.is_(None))
            if kind:
                stmt = stmt.where(MemoryModel.kind == kind)
            stmt = stmt.order_by(MemoryModel.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()

        memories = [r.to_pydantic() for r in rows]
        memories.sort(
            key=lambda m: self.DIRECTION_KIND_PRIORITY.get(m.kind, 99)
        )
        return memories

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Get a memory by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(MemoryModel).where(MemoryModel.id == memory_id))
            row = result.scalar_one_or_none()
            if row is not None:
                # Update access time
                row.accessed_at = utc_now()
                return row.to_pydantic()
            return None

    async def list_memories(
        self,
        scope: str,
        scope_id: str,
        include_invalidated: bool = False,
        kind: str | None = None,
    ) -> list[Memory]:
        """List memories within a scope (memory v2 P1: by default returns only valid entries).

        With include_invalidated=True, includes invalidated entries; when kind is given, filters by kind.
        """
        async with get_session(self._db_url) as session:
            stmt = select(MemoryModel).where(
                MemoryModel.scope == scope,
                MemoryModel.scope_id == scope_id,
            )
            if not include_invalidated:
                stmt = stmt.where(MemoryModel.invalid_at.is_(None))
            if kind:
                stmt = stmt.where(MemoryModel.kind == kind)
            stmt = stmt.order_by(MemoryModel.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [r.to_pydantic() for r in rows]

    async def search_memories(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """Search memories: SQL coarse-recall within scope, then Python BM25 rerank.

        The old whole-string LIKE match missed multi-word queries whose terms
        were not contiguous (e.g. "Python deployment" against "Python backend deployment"). We now
        pull the scope's candidates from SQL (scope isolation unchanged — the
        scope/scope_id filter is the project boundary for memories, which carry
        no project_id column) and rerank them in-process with the built-in BM25.
        """
        # Lazy import keeps the foundational storage module free of the memory
        # package's heavier import graph and avoids any import cycle.
        from aiteam.memory.retriever import bm25_search

        async with get_session(self._db_url) as session:
            # Coarse recall: recency window within scope (BM25 handles precision;
            # a stricter SQL prefilter would risk dropping BM25 hits). The cap
            # keeps rerank O(window) instead of O(scope) as long-lived scopes
            # (team knowledge) accumulate — 500 most-recent is far beyond any
            # limit callers actually use.
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.scope == scope,
                    MemoryModel.scope_id == scope_id,
                    # v2 invalidation semantics: the retrieval read path follows the same rule as the list path —
                    # invalidation does not delete; filtering happens at read time
                    #(review major: previously only the BM25 path missed the filter, making the same endpoint's behavior
                    #self-contradictory)
                    MemoryModel.invalid_at.is_(None),
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(max(500, limit * 20))
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        candidates = [r.to_pydantic() for r in rows]
        if not candidates:
            return []

        # Empty/whitespace query: no ranking signal — return most-recent slice.
        if not query or not query.strip():
            return candidates[:limit]

        ranked = bm25_search(candidates, query)
        return ranked[:limit]

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(MemoryModel).where(MemoryModel.id == memory_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Task memos (memory system v2 P0: the episodic layer promoted to its own table)
    # ================================================================

    async def add_task_memo(
        self,
        task_id: str,
        content: str,
        author: str = "leader",
        memo_type: str = "progress",
        scope_path: str = "",
        project_id: str | None = None,
        supersedes: str | None = None,
    ) -> TaskMemo:
        """Write one task memo to the task_memos table (zero LLM, pure append).

        When supersedes is given, set the superseded row's invalid_at=now and invalidated_by=the new id
        (Zep invalidation semantics: a contradiction/update does not delete the old entry, only invalidates it).
        """
        memo = TaskMemo(
            task_id=task_id,
            project_id=project_id,
            author=author,
            memo_type=memo_type,
            content=content,
            scope_path=scope_path,
        )
        async with get_session(self._db_url) as session:
            session.add(TaskMemoModel.from_pydantic(memo))
            if supersedes:
                res = await session.execute(
                    select(TaskMemoModel).where(TaskMemoModel.id == supersedes)
                )
                old = res.scalar_one_or_none()
                if old is not None and old.invalid_at is None:
                    old.invalid_at = memo.created_at
                    old.invalidated_by = memo.id
        return memo

    async def list_task_memos(
        self, task_id: str, include_invalidated: bool = False
    ) -> list[TaskMemo]:
        """List a task's memos; by default returns only valid entries (invalid_at IS NULL)."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskMemoModel).where(TaskMemoModel.task_id == task_id)
            if not include_invalidated:
                stmt = stmt.where(TaskMemoModel.invalid_at.is_(None))
            stmt = stmt.order_by(TaskMemoModel.created_at.asc(), text("rowid"))
            result = await session.execute(stmt)
            return [r.to_pydantic() for r in result.scalars().all()]

    async def get_task_memo(self, memo_id: str) -> TaskMemo | None:
        """Fetch a single memo by its real id."""
        async with get_session(self._db_url) as session:
            res = await session.execute(
                select(TaskMemoModel).where(TaskMemoModel.id == memo_id)
            )
            row = res.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_project_task_memos(
        self,
        project_id: str,
        scope_path: str | None = None,
        include_invalidated: bool = False,
    ) -> list[TaskMemo]:
        """List all task memos of a project (the corpus source for memory v2 P2 reconcile's coarse screening).

        By default returns only valid entries (invalid_at IS NULL). When scope_path is given, takes only that path
        scope's memos, for on-demand sliced organization.
        """
        async with get_session(self._db_url) as session:
            stmt = select(TaskMemoModel).where(
                TaskMemoModel.project_id == project_id
            )
            if not include_invalidated:
                stmt = stmt.where(TaskMemoModel.invalid_at.is_(None))
            if scope_path is not None:
                stmt = stmt.where(TaskMemoModel.scope_path == scope_path)
            stmt = stmt.order_by(TaskMemoModel.created_at.asc(), text("rowid"))
            result = await session.execute(stmt)
            return [r.to_pydantic() for r in result.scalars().all()]

    async def invalidate_task_memo(
        self, memo_id: str, invalidated_by: str | None = None
    ) -> TaskMemo | None:
        """Explicitly invalidate one task memo (no deletion, Zep invalidation semantics).

        Idempotent: an already-invalidated entry returns its current state directly, without a second rewrite (the base
        for reconcile apply's repeated
        operations returning noop). Returns None when the id does not exist.
        """
        async with get_session(self._db_url) as session:
            res = await session.execute(
                select(TaskMemoModel).where(TaskMemoModel.id == memo_id)
            )
            row = res.scalar_one_or_none()
            if row is None:
                return None
            if row.invalid_at is None:
                row.invalid_at = utc_now()
                row.invalidated_by = invalidated_by
            return row.to_pydantic()

    async def score_task_memo(
        self, memo_id: str, quality_score: int, reason: str = ""
    ) -> TaskMemo | None:
        """Add a quality score to a memo (item 8): quality_score goes into the column, reason into meta. Returns None
        when the id does not exist."""
        async with get_session(self._db_url) as session:
            res = await session.execute(
                select(TaskMemoModel).where(TaskMemoModel.id == memo_id)
            )
            row = res.scalar_one_or_none()
            if row is None:
                return None
            row.quality_score = quality_score
            # Reassign a new dict to trigger the JSON column's dirty detection (plain JSON has no MutableDict tracking).
            meta = dict(row.meta) if isinstance(row.meta, dict) else {}
            if reason:
                meta["quality_reason"] = reason
            row.meta = meta
            return row.to_pydantic()

    async def count_valid_task_memos_since(
        self, project_id: str, since: datetime | None
    ) -> int:
        """Count valid task memos created in a project after since (for the volume-threshold soft hint).

        When since is None, counts all valid entries.
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(func.count())
                .select_from(TaskMemoModel)
                .where(
                    TaskMemoModel.project_id == project_id,
                    TaskMemoModel.invalid_at.is_(None),
                )
            )
            if since is not None:
                stmt = stmt.where(TaskMemoModel.created_at > since)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)

    async def get_last_reconcile_at(self, project_id: str) -> datetime | None:
        """Read a project's last organization time (stored in project.config['memory']['last_reconcile_at'])."""
        project = await self.get_project(project_id)
        if project is None:
            return None
        raw = (project.config or {}).get("memory", {}).get("last_reconcile_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    async def set_last_reconcile_at(
        self, project_id: str, when: datetime | None = None
    ) -> datetime | None:
        """Record a project's organization timestamp into project.config (reusing the existing config storage; no new
        table)."""
        when = when or utc_now()
        async with get_session(self._db_url) as session:
            res = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = res.scalar_one_or_none()
            if row is None:
                return None
            cfg = dict(row.config) if isinstance(row.config, dict) else {}
            mem = dict(cfg.get("memory") or {})
            mem["last_reconcile_at"] = when.isoformat()
            cfg["memory"] = mem
            row.config = cfg
            row.updated_at = utc_now()
        return when

    async def _hydrate_task_memos(
        self, session: Any, tasks: list[Task]
    ) -> None:
        """Assemble the valid memos from the task_memos table back into each task's config['memo'] view field.

        After the table promotion, the write path goes only to the table and the original JSON in config is frozen as an
        archive. This lets all consumers still reading
        task.config['memo'] (completion verification / completion-gate hook / failure diagnosis /
        replay / frontend, etc.) obtain valid memos with zero changes, with real ids and invalidated entries filtered
        out.
        """
        ids = [t.id for t in tasks]
        if not ids:
            return
        stmt = (
            select(TaskMemoModel)
            .where(
                TaskMemoModel.task_id.in_(ids),
                TaskMemoModel.invalid_at.is_(None),
            )
            .order_by(TaskMemoModel.created_at.asc(), text("rowid"))
        )
        rows = (await session.execute(stmt)).scalars().all()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r.task_id, []).append(
                _task_memo_to_legacy(r.to_pydantic())
            )
        # Only overwrite the config['memo'] view when the table holds a valid memo for that task; when the table has
        # none,
        # keep the original config value (after the promotion the write path goes only to the table and never writes
        # config.memo again, so production
        # leaves no dirty memos behind; this fallback only keeps the "direct config-writing legacy/test paths"
        # compatible).
        for t in tasks:
            memos = grouped.get(t.id)
            if not memos:
                continue
            cfg = dict(t.config) if isinstance(t.config, dict) else {}
            cfg["memo"] = memos
            t.config = cfg

    async def backfill_task_memos_from_config(self) -> int:
        """Memory system v2 P0 one-time backfill: tasks.config['memo'] arrays → task_memos rows.

        Two layers of idempotency (review major: this repo has a history of dual uvicorn instances coexisting, and
        check-then-insert
        is not atomic across processes and once could produce 2N duplicate rows):
        1) Fast path: skip when the table is non-empty;
        2) Race fallback: row ids are deterministically generated with uuid5(task_id, array index) — config.memo
          is frozen as an archive so indices are stable; two instances backfilling simultaneously land on the same batch
          of PKs, and INSERT OR IGNORE
          makes the later one silently skip row by row, conserving the row count.
        The original JSON is kept untouched (archive).
        """
        import uuid as _uuid

        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        async with get_session(self._db_url) as session:
            existing = await session.execute(
                select(func.count()).select_from(TaskMemoModel)
            )
            if (existing.scalar() or 0) > 0:
                return 0  # a non-empty table = already migrated; do not run again

            rows = (await session.execute(select(TaskModel))).scalars().all()
            inserted = 0
            for row in rows:
                cfg = row.config if isinstance(row.config, dict) else {}
                memos = cfg.get("memo") or []
                if not isinstance(memos, list):
                    continue
                for idx, m in enumerate(memos):
                    if not isinstance(m, dict):
                        continue
                    content = m.get("content") or ""
                    if not content:
                        continue
                    created = _parse_memo_timestamp(m.get("timestamp"))
                    det_id = str(
                        _uuid.uuid5(
                            _uuid.NAMESPACE_URL,
                            f"aiteam:task-memo-backfill:{row.id}:{idx}",
                        )
                    )
                    stmt = (
                        _sqlite_insert(TaskMemoModel)
                        .values(
                            id=det_id,
                            task_id=row.id,
                            project_id=row.project_id,
                            author=m.get("author") or "leader",
                            memo_type=m.get("type") or "progress",
                            content=content,
                            scope_path="",
                            quality_score=None,
                            invalid_at=None,
                            invalidated_by=None,
                            meta={},
                            created_at=created,
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    result = await session.execute(stmt)
                    inserted += result.rowcount or 0
            return inserted

    async def list_team_knowledge(
        self,
        team_id: str,
        memory_type: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List the knowledge a team can draw on, with optional type filtering.

        Reads two scopes and merges them newest-first:
          * ``scope=team, scope_id=team_id`` — the historical home of failure
            alchemy output, plus anything explicitly filed against this team;
          * ``scope=project, scope_id=<the team's project>`` — where failure
            alchemy files new lessons since 2026-07-27 (teams are session-level
            containers and die young; the project is what outlives them).
        Reading both keeps this endpoint whole across that cut-over — the 125
        pre-existing team-scoped rows were deliberately NOT migrated.

        Args:
            team_id: Team ID
            memory_type: Optional type filter, matches metadata.type field
                         e.g., failure_alchemy / lesson_learned / loop_review
            limit: Maximum number of results to return
        """
        team = await self.get_team(team_id) or await self.get_team_by_name(team_id)
        project_id = team.project_id if team else None
        resolved_team_id = team.id if team else team_id

        conditions = [
            (MemoryModel.scope == MemoryScope.TEAM.value)
            & (MemoryModel.scope_id == resolved_team_id)
        ]
        if project_id:
            conditions.append(
                (MemoryModel.scope == MemoryScope.PROJECT.value)
                & (MemoryModel.scope_id == project_id)
            )

        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .where(or_(*conditions))
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            memories = [r.to_pydantic() for r in rows]

        if memory_type:
            memories = [m for m in memories if m.metadata.get("type") == memory_type]
        return memories

    async def list_agent_experience(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[Memory]:
        """List an Agent's experience memories (scope=agent).

        Args:
            agent_id: Agent ID
            limit: Maximum number of results to return
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.scope == MemoryScope.AGENT.value,
                    MemoryModel.scope_id == agent_id,
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Meetings
    # ================================================================

    async def create_meeting(
        self,
        team_id: str,
        topic: str,
        participants: list[str] | None = None,
        meta_json: dict | None = None,
    ) -> Meeting:
        """Create a meeting."""
        meeting = Meeting(
            team_id=team_id,
            topic=topic,
            participants=participants or [],
            meta_json=meta_json or {},
        )
        orm = MeetingModel.from_pydantic(meeting)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return meeting

    async def get_meeting(self, meeting_id: str) -> Meeting | None:
        """Get a meeting by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_meetings(
        self,
        team_id: str,
        status: MeetingStatus | None = None,
    ) -> list[Meeting]:
        """List team meetings, optionally filtered by status."""
        async with get_session(self._db_url) as session:
            stmt = select(MeetingModel).where(MeetingModel.team_id == team_id)
            stmt = self._apply_project_filter(stmt, MeetingModel)
            if status is not None:
                stmt = stmt.where(MeetingModel.status == status.value)
            stmt = stmt.order_by(MeetingModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_meeting(self, meeting_id: str, **kwargs: object) -> Meeting:
        """Update meeting information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Meeting {meeting_id} not found"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, MeetingStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    MeetingStatus(status_val)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def create_meeting_message(
        self,
        meeting_id: str,
        agent_id: str,
        agent_name: str,
        content: str,
        round_number: int = 1,
        msg_metadata: dict | None = None,
    ) -> MeetingMessage:
        """Create a meeting message."""
        message = MeetingMessage(
            meeting_id=meeting_id,
            agent_id=agent_id,
            agent_name=agent_name,
            content=content,
            round_number=round_number,
            msg_metadata=msg_metadata or {},
        )
        orm = MeetingMessageModel.from_pydantic(message)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return message

    async def list_meeting_messages(
        self,
        meeting_id: str,
        limit: int = 100,
    ) -> list[MeetingMessage]:
        """List meeting messages."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(MeetingMessageModel)
                .where(MeetingMessageModel.meeting_id == meeting_id)
                .order_by(MeetingMessageModel.timestamp)
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_expired_meetings(self, hours: int = 24) -> list[Meeting]:
        """Get active meetings with no new messages for more than the specified hours.

        Determination logic:
        - Meetings with messages: last message timestamp is more than `hours` ago
        - Meetings without messages: creation time is more than `hours` ago
        """
        cutoff = utc_now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # Subquery: last message time for each meeting
            last_msg_subq = (
                select(
                    MeetingMessageModel.meeting_id,
                    func.max(MeetingMessageModel.timestamp).label("last_msg_time"),
                )
                .group_by(MeetingMessageModel.meeting_id)
                .subquery()
            )

            # Main query: active meetings LEFT JOIN last message time
            stmt = (
                select(MeetingModel)
                .outerjoin(
                    last_msg_subq,
                    MeetingModel.id == last_msg_subq.c.meeting_id,
                )
                .where(
                    MeetingModel.status == MeetingStatus.ACTIVE.value,
                    # Use last message time if available, otherwise use creation time
                    func.coalesce(
                        last_msg_subq.c.last_msg_time,
                        MeetingModel.created_at,
                    )
                    < cutoff,
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def conclude_meeting(self, meeting_id: str) -> Meeting | None:
        """Conclude a meeting, setting its status to concluded."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = MeetingStatus.CONCLUDED.value
            row.concluded_at = utc_now()
            return row.to_pydantic()

    # ================================================================
    # Hooks — CC session-related queries
    # ================================================================

    async def find_agents_by_session_and_name(
        self,
        session_id: str,
        agent_name: str,
    ) -> list[Agent]:
        """All rows matching (session, name), newest first.

        Dedup callers need to see **every** candidate, not an arbitrary one: the
        row they may reuse must be chosen by dispatch identity (cc_tool_use_id +
        token ledger, see ``services.agent_identity``), and that decision cannot be
        made from a single row picked at random by the database.
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(
                    AgentModel.session_id == session_id,
                    AgentModel.name == agent_name,
                )
                .order_by(AgentModel.created_at.desc(), AgentModel.id.desc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_agent_by_session(
        self,
        session_id: str,
        agent_name: str,
    ) -> Agent | None:
        """Find a registered Agent by CC session ID and Agent name.

        Returns the **newest** match. The previous implementation was ``limit(1)``
        with no ORDER BY, i.e. "whichever row the engine happened to hand back
        first" - and every caller here wants the row that belongs to the current
        spawn (whoami resolution, TeammateIdle observation), which is the newest
        one. Ordering is not a nicety: an ambiguous answer here used to decide
        which row a new dispatch got welded onto (A-06).
        """
        rows = await self.find_agents_by_session_and_name(session_id, agent_name)
        return rows[0] if rows else None

    async def find_agents_by_session(self, session_id: str) -> list[Agent]:
        """Find all Agents associated with a CC session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.session_id == session_id)
                .order_by(AgentModel.created_at)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_agent_by_cc_id(self, cc_agent_id: str) -> Agent | None:
        """Find an Agent by CC internal agent_id."""
        async with get_session(self._db_url) as session:
            stmt = select(AgentModel).where(AgentModel.cc_tool_use_id == cc_agent_id).limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_agents_by_role(self, role: str) -> list[Agent]:
        """Find all Agents by role (across teams)."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.role == role)
                .order_by(AgentModel.created_at.desc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def count_agents_by_source(
        self,
        source: str,
        session_id: str | None = None,
    ) -> int:
        """Count Agents by source, optionally filtered by session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(func.count())
                .select_from(AgentModel)
                .where(
                    AgentModel.source == source,
                )
            )
            if session_id is not None:
                stmt = stmt.where(AgentModel.session_id == session_id)
            result = await session.execute(stmt)
            return result.scalar_one()

    # ================================================================
    # Agent Activities — tool call activity logs
    # ================================================================

    async def create_activity(
        self,
        agent_id: str,
        session_id: str,
        tool_name: str,
        input_summary: str = "",
        output_summary: str = "",
        status: str = "completed",
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> AgentActivity:
        """Record a single tool call activity for an Agent."""
        activity = AgentActivity(
            agent_id=agent_id,
            session_id=session_id,
            tool_name=tool_name,
            input_summary=input_summary[:500],
            output_summary=output_summary[:500],
            status=status,
            duration_ms=duration_ms,
            error=error,
        )
        orm = AgentActivityModel.from_pydantic(activity)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return activity

    async def find_running_activity(
        self,
        agent_id: str,
        session_id: str,
        tool_name: str,
    ) -> AgentActivity | None:
        """Find a matching running-status activity (Pre→Post correlation)."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(
                    AgentActivityModel.agent_id == agent_id,
                    AgentActivityModel.session_id == session_id,
                    AgentActivityModel.tool_name == tool_name,
                    AgentActivityModel.status == "running",
                )
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_activity(
        self,
        activity_id: str,
        **kwargs: Any,
    ) -> None:
        """Update activity fields (used by PostToolUse to backfill duration_ms/status/output)."""
        async with get_session(self._db_url) as session:
            stmt = select(AgentActivityModel).where(AgentActivityModel.id == activity_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            session.add(row)

    async def list_activities(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[AgentActivity]:
        """Get an Agent's activity log."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(AgentActivityModel.agent_id == agent_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_activities_by_session(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[AgentActivity]:
        """Get all activities under a session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(AgentActivityModel.session_id == session_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_activities_by_team(
        self,
        team_id: str,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[AgentActivity]:
        """Get activity logs for all agents under a team, sorted by timestamp descending."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                .where(AgentModel.team_id == team_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            if agent_id is not None:
                stmt = stmt.where(AgentActivityModel.agent_id == agent_id)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Analytics — aggregate statistics queries
    # ================================================================

    async def aggregate_activity_stats_by_role(
        self, error_sample_limit: int = 500
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Aggregate every agent activity by the owning agent's role — in 2 queries.

        Prompt-effectiveness stats used to be assembled by looping over every team
        and issuing three queries each (agents + activities + knowledge). On this
        install that is 254 teams x 3 = 762 round-trips per page load, and it also
        pulled up to 2000 activity ROWS per team into Python just to count them.

        Returns:
            (stats, error_samples) where stats is one dict per role with
            total/success/failure/total_duration_ms/duration_samples, and
            error_samples is a bounded list of (role, error) for failed rows.
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentModel.role.label("role"),
                    func.count(AgentActivityModel.id).label("total"),
                    func.sum(
                        case((AgentActivityModel.status == "completed", 1), else_=0)
                    ).label("success"),
                    func.sum(
                        case(
                            (AgentActivityModel.status.in_(("failed", "error")), 1),
                            else_=0,
                        )
                    ).label("failure"),
                    func.coalesce(func.sum(AgentActivityModel.duration_ms), 0).label(
                        "total_duration_ms"
                    ),
                    func.count(AgentActivityModel.duration_ms).label("duration_samples"),
                )
                .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                .group_by(AgentModel.role)
            )
            rows = (await session.execute(stmt)).all()
            stats = [
                {
                    "role": r.role or "",
                    "total": int(r.total or 0),
                    "success": int(r.success or 0),
                    "failure": int(r.failure or 0),
                    "total_duration_ms": int(r.total_duration_ms or 0),
                    "duration_samples": int(r.duration_samples or 0),
                }
                for r in rows
            ]

            err_stmt = (
                select(AgentModel.role, AgentActivityModel.error)
                .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                .where(
                    AgentActivityModel.status.in_(("failed", "error")),
                    AgentActivityModel.error.isnot(None),
                )
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(error_sample_limit)
            )
            err_rows = (await session.execute(err_stmt)).all()
            errors = [(r[0] or "", r[1] or "") for r in err_rows]
        return stats, errors

    async def list_memories_by_metadata_type(
        self, memory_type: str, limit: int = 2000
    ) -> list[Memory]:
        """All memories whose ``metadata.type`` equals *memory_type*, newest first.

        One query instead of one per team (see aggregate_activity_stats_by_role).
        The JSON predicate is pushed down when the backend supports it and falls
        back to a Python filter otherwise, so behaviour never depends on the
        SQLite build's JSON1 availability.
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            try:
                stmt = stmt.where(
                    MemoryModel.metadata_json["type"].as_string() == memory_type
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [r.to_pydantic() for r in rows]
            except Exception:
                pass  # JSON path filter unavailable on this backend — scan below
            rows = (
                await session.execute(
                    select(MemoryModel).order_by(MemoryModel.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        return [
            m
            for m in (r.to_pydantic() for r in rows)
            if (m.metadata or {}).get("type") == memory_type
        ]

    async def count_activities_by_tool(
        self,
        agent_id: str | None = None,
        team_id: str | None = None,
    ) -> list[dict]:
        """Count activities grouped by tool name."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentActivityModel.tool_name,
                    func.count().label("count"),
                )
                .group_by(AgentActivityModel.tool_name)
                .order_by(func.count().desc())
            )
            if agent_id is not None:
                stmt = stmt.where(AgentActivityModel.agent_id == agent_id)
            if team_id is not None:
                # Filter by team via agent table join
                stmt = stmt.join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                ).where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            return [{"tool_name": row.tool_name, "count": row.count} for row in result.all()]

    async def get_activity_timeline(
        self,
        team_id: str | None = None,
        hours: int = 24,
    ) -> list[dict]:
        """Count activities by hour (last N hours)."""
        cutoff = utc_now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # Hour granularity via strftime (SQLite-compatible). The stored value is
            # UTC, so the bucket label is spelled with an explicit "+00:00": a bare
            # "2026-07-28 09:00" would be read as local time by the browser and slide
            # the whole chart by the viewer's offset.
            hour_expr = func.strftime(
                "%Y-%m-%dT%H:00:00+00:00", AgentActivityModel.timestamp
            )
            stmt = (
                select(
                    hour_expr.label("hour"),
                    func.count().label("count"),
                )
                .where(AgentActivityModel.timestamp >= cutoff)
                .group_by(hour_expr)
                .order_by(hour_expr)
            )
            if team_id is not None:
                stmt = stmt.join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                ).where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            return [{"hour": row.hour, "count": row.count} for row in result.all()]

    async def get_agent_productivity(
        self,
        team_id: str | None = None,
    ) -> list[dict]:
        """Productivity metrics per Agent: activity count, tool diversity, last active time."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentActivityModel.agent_id,
                    AgentModel.name.label("agent_name"),
                    func.count().label("activity_count"),
                    func.count(AgentActivityModel.tool_name.distinct()).label("tools_used"),
                    func.max(AgentActivityModel.timestamp).label("last_active"),
                )
                .join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                )
                .group_by(AgentActivityModel.agent_id, AgentModel.name)
                .order_by(func.count().desc())
            )
            if team_id is not None:
                stmt = stmt.where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "agent_id": row.agent_id,
                    "agent_name": row.agent_name or "unknown",
                    "activity_count": row.activity_count,
                    "tools_used": row.tools_used,
                    "last_active": row.last_active.isoformat() if row.last_active else None,
                }
                for row in rows
            ]

    async def get_task_completion_stats(
        self,
        team_id: str | None = None,
    ) -> dict:
        """Task completion rate and average completion time statistics."""
        async with get_session(self._db_url) as session:
            stmt = select(
                func.count().label("total"),
                func.sum(
                    case(
                        (TaskModel.status == TaskStatus.COMPLETED.value, 1),
                        else_=0,
                    )
                ).label("completed"),
                func.avg(
                    case(
                        (
                            TaskModel.completed_at.isnot(None),
                            func.julianday(TaskModel.completed_at)
                            - func.julianday(TaskModel.created_at),
                        ),
                        else_=None,
                    )
                ).label("avg_completion_days"),
            ).select_from(TaskModel)

            if team_id is not None:
                stmt = stmt.where(TaskModel.team_id == team_id)

            result = await session.execute(stmt)
            row = result.one()

            total = row.total or 0
            completed = row.completed or 0
            avg_days = row.avg_completion_days

            return {
                "total_tasks": total,
                "completed_tasks": completed,
                "completion_rate": round(completed / total, 4) if total > 0 else 0,
                "avg_completion_hours": round(avg_days * 24, 2) if avg_days else None,
            }

    async def get_agent_utilization(
        self,
        team_id: str | None = None,
    ) -> list[dict]:
        """Agent utilization: calculate active period ratio based on activity timestamps."""
        async with get_session(self._db_url) as session:
            # Query each Agent's activity time span and count (JOIN to get name, avoid N+1)
            stmt = (
                select(
                    AgentActivityModel.agent_id,
                    AgentModel.name.label("agent_name"),
                    func.count().label("activity_count"),
                    func.min(AgentActivityModel.timestamp).label("first_active"),
                    func.max(AgentActivityModel.timestamp).label("last_active"),
                    func.count(AgentActivityModel.tool_name.distinct()).label("tools_used"),
                )
                .join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                )
                .group_by(AgentActivityModel.agent_id, AgentModel.name)
                .order_by(func.count().desc())
            )
            if team_id is not None:
                stmt = stmt.where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            rows = result.all()

            output: list[dict] = []
            for row in rows:
                # Estimate utilization: activity density (activities per hour)
                span_hours = 0.0
                if row.first_active and row.last_active:
                    span = (row.last_active - row.first_active).total_seconds() / 3600
                    span_hours = max(span, 1.0)  # Minimum 1 hour

                utilization = round(row.activity_count / span_hours, 2) if span_hours > 0 else 0

                output.append(
                    {
                        "agent_id": row.agent_id,
                        "agent_name": row.agent_name or "unknown",
                        "activity_count": row.activity_count,
                        "tools_used": row.tools_used,
                        "span_hours": round(span_hours, 2),
                        "activities_per_hour": utilization,
                        "first_active": row.first_active.isoformat() if row.first_active else None,
                        "last_active": row.last_active.isoformat() if row.last_active else None,
                    }
                )
            return output

    # ================================================================
    # Cross-project messages — always in the global default DB
    # ================================================================

    async def list_cross_messages(
        self,
        project_id: str,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[CrossMessage]:
        """List inbox messages for a project — READ-ONLY ARCHIVE ACCESSOR.

        Cross-project messaging was retired 2026-07-27 (batch 8a): the two MCP
        tools and /api/cross-messages are gone, and the two writers
        (create_cross_message / mark_cross_message_read) with them. The table is
        frozen, not dropped — the same treatment as tasks.config.memo — so this
        reader stays for anyone who needs to look at the history.

        Returns messages where to_project_id == project_id (direct)
        OR to_project_id IS NULL (broadcast), sorted newest-first.

        Args:
            project_id: Recipient project's 12-char ID.
            unread_only: If True, exclude messages that have been read.
            limit: Maximum number of results to return.

        Returns:
            List of CrossMessage sorted by created_at descending.
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = (
                select(CrossMessageModel)
                .where(
                    or_(
                        CrossMessageModel.to_project_id == project_id,
                        CrossMessageModel.to_project_id.is_(None),
                    )
                )
                .order_by(CrossMessageModel.created_at.desc())
                .limit(limit)
            )
            if unread_only:
                stmt = stmt.where(CrossMessageModel.read_at.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def count_unread_cross_messages(self, project_id: str) -> int:
        """Count unread messages in a project's inbox (direct + broadcast).

        Args:
            project_id: Recipient project's 12-char ID.

        Returns:
            Number of unread messages.
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = (
                select(func.count())
                .select_from(CrossMessageModel)
                .where(
                    or_(
                        CrossMessageModel.to_project_id == project_id,
                        CrossMessageModel.to_project_id.is_(None),
                    ),
                    CrossMessageModel.read_at.is_(None),
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one() or 0

    # ================================================================
    # Scheduled Tasks
    # ================================================================

    async def create_scheduled_task(
        self,
        name: str,
        interval_seconds: int,
        action_type: str,
        next_run_at: datetime,
        team_id: str | None = None,
        description: str = "",
        action_config: dict | None = None,
    ) -> ScheduledTask:
        """Create a scheduled task."""
        task = ScheduledTask(
            team_id=team_id,
            name=name,
            description=description,
            interval_seconds=interval_seconds,
            action_type=action_type,
            action_config=action_config or {},
            next_run_at=next_run_at,
        )
        orm = ScheduledTaskModel.from_pydantic(task)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return task

    async def list_scheduled_tasks(self, team_id: str | None = None) -> list[ScheduledTask]:
        """List scheduled tasks, optionally filtered by team_id."""
        async with get_session(self._db_url) as session:
            stmt = select(ScheduledTaskModel).order_by(ScheduledTaskModel.created_at.desc())
            if team_id is not None:
                stmt = stmt.where(ScheduledTaskModel.team_id == team_id)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_scheduled_task(self, task_id: str) -> ScheduledTask | None:
        """Get a scheduled task by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_scheduled_task(self, task_id: str, **kwargs: Any) -> ScheduledTask | None:
        """Update a scheduled task."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            return row.to_pydantic()

    async def delete_scheduled_task(self, task_id: str) -> bool:
        """Delete a scheduled task."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Wake Sessions
    # ================================================================

    async def create_wake_session(
        self, scheduled_task_id: str, agent_name: str, team_id: str = ""
    ) -> WakeSession:
        """Create a new wake session record."""
        ws = WakeSession(
            scheduled_task_id=scheduled_task_id,
            agent_name=agent_name,
            team_id=team_id,
        )
        orm = WakeSessionModel.from_pydantic(ws)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return ws

    async def update_wake_session(self, session_id: str, **kwargs: Any) -> WakeSession | None:
        """Update wake session fields (finished_at, outcome, exit_code, etc)."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WakeSessionModel).where(WakeSessionModel.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            return row.to_pydantic()

    async def get_recent_wake_sessions(
        self, agent_name: str, limit: int = 30
    ) -> list[WakeSession]:
        """Get recent wake sessions for an agent, ordered by started_at desc."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WakeSessionModel)
                .where(WakeSessionModel.agent_name == agent_name)
                .order_by(WakeSessionModel.started_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_consecutive_failures(self, agent_name: str) -> int:
        """Count consecutive real failures for an agent (circuit breaker logic).

        Only 'error' and 'timeout' count as failures. Skips like
        'skipped_triage', 'skipped_concurrent', 'cancelled' are ignored.
        """
        _failure_outcomes = {"error", "timeout"}
        sessions = await self.get_recent_wake_sessions(agent_name, limit=30)
        count = 0
        for s in sessions:
            if s.outcome in _failure_outcomes:
                count += 1
            elif s.outcome == "completed":
                break
            # skip non-failure, non-completed outcomes (triage skip, concurrent skip, etc.)
        return count

    async def has_actionable_tasks(self, agent_name: str) -> tuple[bool, str]:
        """Check if agent has pending/running tasks assigned to them."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TaskModel).where(
                    TaskModel.assigned_to == agent_name,
                    TaskModel.status.in_(["pending", "running"]),
                )
            )
            tasks = result.scalars().all()
            if tasks:
                return True, f"{len(tasks)} actionable tasks"
            return False, "no actionable tasks"

    async def toggle_wake_agents(self, enabled: bool) -> int:
        """Enable/disable all wake_agent scheduled tasks. Returns count affected."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(
                    ScheduledTaskModel.action_type == "wake_agent",
                    ScheduledTaskModel.enabled == (not enabled),
                )
            )
            tasks = result.scalars().all()
            for t in tasks:
                t.enabled = enabled
            await session.commit()
            return len(tasks)

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """Delete wake sessions older than specified days. Returns count deleted."""
        cutoff = utc_now() - timedelta(days=days)
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(WakeSessionModel).where(WakeSessionModel.started_at < cutoff)
            )
            return result.rowcount  # type: ignore[union-attr]

    async def get_due_tasks(self, now: datetime) -> list[ScheduledTask]:
        """Get all enabled scheduled tasks whose next_run_at <= now."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(
                    ScheduledTaskModel.enabled == True,  # noqa: E712
                    ScheduledTaskModel.next_run_at <= now,
                )
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Leader Briefings
    # ================================================================

    async def create_briefing(
        self,
        title: str,
        description: str = "",
        options: str = "",
        recommendation: str = "",
        urgency: str = "medium",
        project_id: str = "",
        tags: list[str] | None = None,
    ) -> LeaderBriefing:
        """Create a new leader briefing item."""
        briefing = LeaderBriefing(
            title=title,
            description=description,
            options=options,
            recommendation=recommendation,
            urgency=urgency,
            project_id=project_id,
            tags=tags or [],
        )
        orm = LeaderBriefingModel.from_pydantic(briefing)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return briefing

    async def list_briefings(
        self, status: str = "pending", project_id: str = "", tag: str = ""
    ) -> list[LeaderBriefing]:
        """List briefing items, optionally filtered by status, project_id and tag."""
        async with get_session(self._db_url) as session:
            conditions = []
            if status and status != "all":
                conditions.append(LeaderBriefingModel.status == status)
            if project_id:
                conditions.append(LeaderBriefingModel.project_id == project_id)
            stmt = select(LeaderBriefingModel).order_by(
                LeaderBriefingModel.created_at.desc()
            )
            # Apply universal project isolation
            stmt = self._apply_project_filter(stmt, LeaderBriefingModel)
            if conditions:
                stmt = stmt.where(*conditions)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            items = [r.to_pydantic() for r in rows]
        if tag:
            # Tag comparison does whole-value matching on the Python side: tags is a JSON column, and LIKE '%tag%'
            # would match "release" onto "release-candidate" (a substring false hit). The queue's scale is
            # human decision items, so the cost of fetching the whole table and filtering is negligible.
            items = [b for b in items if tag in (b.tags or [])]
        return items

    async def resolve_briefing(
        self, briefing_id: str, resolution: str, status: str = "resolved"
    ) -> LeaderBriefing | None:
        """Resolve a briefing item with user's decision."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(LeaderBriefingModel).where(LeaderBriefingModel.id == briefing_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.resolution = resolution
            row.status = status
            row.resolved_at = utc_now()
            return row.to_pydantic()

    async def dismiss_briefing(self, briefing_id: str) -> LeaderBriefing | None:
        """Dismiss a briefing item without a resolution."""
        return await self.resolve_briefing(briefing_id, resolution="", status="dismissed")

    # ================================================================
    # Channel Messages (v1.0 P1-6)
    # ================================================================

    async def create_channel_message(
        self,
        channel: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
        metadata: dict | None = None,
    ) -> ChannelMessage:
        """Create a channel message."""
        msg = ChannelMessage(
            channel=channel,
            sender=sender,
            content=content,
            mentions=mentions or [],
            metadata=metadata or {},
        )
        orm = ChannelMessageModel.from_pydantic(msg)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return msg

    async def list_channel_messages(
        self,
        channel: str,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ChannelMessage]:
        """List messages in a channel, optionally filtered by since timestamp."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(ChannelMessageModel)
                .where(ChannelMessageModel.channel == channel)
                .order_by(ChannelMessageModel.created_at.asc())
                .limit(limit)
            )
            if since is not None:
                stmt = stmt.where(ChannelMessageModel.created_at > since)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_channel_mentions(
        self,
        agent_name: str,
        limit: int = 50,
    ) -> list[ChannelMessage]:
        """List channel messages that mention a specific agent."""
        mention_tag = f"@{agent_name}"
        async with get_session(self._db_url) as session:
            # SQLite JSON contains check via LIKE on serialized JSON string
            stmt = (
                select(ChannelMessageModel)
                .where(
                    ChannelMessageModel.mentions.cast(SAString).contains(mention_tag)
                )
                .order_by(ChannelMessageModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ── Reports ────────────────────────────────────────────────

    async def create_report(self, report: Report) -> Report:
        """Create a new report."""
        async with get_session(self._db_url) as session:
            row = ReportModel.from_pydantic(report)
            session.add(row)
            return report

    async def get_report(self, report_id: str) -> Report | None:
        """Get a single report by ID."""
        async with get_session(self._db_url) as session:
            row = await session.get(ReportModel, report_id)
            return row.to_pydantic() if row else None

    async def list_reports(
        self,
        project_id: str = "",
        report_type: str = "",
        author: str = "",
        topic: str = "",
        limit: int = 50,
    ) -> list[Report]:
        """List reports with optional filters."""
        async with get_session(self._db_url) as session:
            stmt = select(ReportModel)
            if project_id:
                stmt = stmt.where(ReportModel.project_id == project_id)
            if report_type:
                stmt = stmt.where(ReportModel.report_type == report_type)
            if author:
                stmt = stmt.where(ReportModel.author == author)
            if topic:
                stmt = stmt.where(ReportModel.topic.contains(topic))
            stmt = stmt.order_by(ReportModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def delete_report(self, report_id: str) -> bool:
        """Delete a report."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ReportModel).where(ReportModel.id == report_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    async def delete_reports_by_project(self, project_id: str) -> int:
        """Delete all reports for a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ReportModel).where(ReportModel.project_id == project_id)
            )
            return result.rowcount or 0  # type: ignore[union-attr]

    # ================================================================
    # Ecosystem repo profiles
    # ================================================================

    def _effective_project_id(
        self, explicit: str | None
    ) -> str | None:
        """Resolve project_id used to scope ecosystem rows.

        Precedence: explicit argument > _project_scope (set on repo init) > None.
        Empty string is normalised to None so callers can pass "" to mean
        "no override".
        """
        if explicit:
            return explicit
        if self._project_scope:
            return self._project_scope
        return None

    async def upsert_ecosystem_profile(
        self,
        profile: EcosystemRepoProfile,
        project_id: str | None = None,
    ) -> None:
        """Upsert an ecosystem repo profile by the (project_id, repo_full_name) unique key, updating dynamic fields.

        Args:
            profile: the ecosystem repo profile Pydantic model to write.
            project_id: explicitly specified project scope; when empty, falls back to the current repo's _project_scope,
                        and a final None means global/unowned (compatible with legacy data).
        """
        import json

        now = profile.last_scanned_at
        effective_pid = self._effective_project_id(project_id) or profile.project_id
        # Stamp project_id back onto the Pydantic so from_pydantic carries it.
        if effective_pid and not profile.project_id:
            profile.project_id = effective_pid

        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.repo_full_name == profile.repo_full_name
            )
            if effective_pid is None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id.is_(None))
            else:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                session.add(EcosystemRepoProfileModel.from_pydantic(profile))
            else:
                # Update dynamic fields; preserve first_seen_at + project_id
                row.stars = profile.stars
                row.description = profile.description
                row.language = profile.language
                row.topics = json.dumps(profile.topics) if profile.topics else None
                row.homepage = profile.homepage
                row.last_commit_at = profile.last_commit_at
                row.needs_deep_review = profile.needs_deep_review
                row.relevance_category = profile.relevance_category
                row.relevance_score = profile.relevance_score
                row.one_line_summary = profile.one_line_summary
                row.last_scanned_at = now
                # Stage B fields: dynamically updatable
                row.pushed_at = profile.pushed_at
                row.is_archived = profile.is_archived
                row.scan_run_id = profile.scan_run_id
                row.description_excerpt = profile.description_excerpt or ""
                # P1.C-1: persist discovered_via_queries when provided (union handled by scanner)
                if profile.discovered_via_queries:
                    row.discovered_via_queries = json.dumps(profile.discovered_via_queries)

    async def search_ecosystem_profiles(
        self,
        keyword: str = "",
        topic: str = "",
        min_stars: int = 0,
        max_stars: int | None = None,
        needs_deep_review: bool | None = None,
        category: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[EcosystemRepoProfile]:
        """Filter ecosystem repo profiles by field, supporting keywords, topics, star ranges, and deep-review flags.

        Args:
            project_id: explicitly specified project scope; when empty, _project_scope injects it automatically.
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel)
            stmt = self._apply_project_filter(stmt, EcosystemRepoProfileModel)
            if project_id:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.project_id == project_id
                )

            if min_stars > 0:
                stmt = stmt.where(EcosystemRepoProfileModel.stars >= min_stars)
            if max_stars is not None and max_stars > 0:
                stmt = stmt.where(EcosystemRepoProfileModel.stars <= max_stars)
            if needs_deep_review is not None:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.needs_deep_review == needs_deep_review
                )
            if category:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.relevance_category == category
                )
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        EcosystemRepoProfileModel.name.ilike(kw),
                        EcosystemRepoProfileModel.description.ilike(kw),
                        EcosystemRepoProfileModel.one_line_summary.ilike(kw),
                        EcosystemRepoProfileModel.repo_full_name.ilike(kw),
                    )
                )
            if topic:
                tp = f"%{topic}%"
                stmt = stmt.where(EcosystemRepoProfileModel.topics.ilike(tp))

            stmt = stmt.order_by(EcosystemRepoProfileModel.stars.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_ecosystem_profile(
        self,
        repo_full_name: str,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Fetch a single ecosystem repo profile by repo_full_name.

        Args:
            project_id: explicit scope; when empty, falls back to _project_scope.
                If both are empty, matches project_id IS NULL (global rows).
        """
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.repo_full_name == repo_full_name
            )
            if effective_pid is None and self._project_scope == "":
                # No scope at all — return any (global lookup, used by
                # cross-project tooling and tests).
                pass
            elif effective_pid is None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id.is_(None))
            else:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def get_ecosystem_profile_by_id(
        self,
        repo_id: str,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Fetch a single ecosystem repo profile by primary-key id.

        Args:
            project_id: explicit scope; when empty, falls back to _project_scope.
                When the scope is non-empty, matches only the same project's id, avoiding cross-project leakage.
        """
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.id == repo_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    # ================================================================
    # Ecosystem deep reviews
    # ================================================================

    async def create_deep_review(
        self,
        review: EcosystemDeepReview,
        project_id: str | None = None,
    ) -> EcosystemDeepReview:
        """Create a deep-scan report record.

        D5 convergence choke point 1: ``status`` is a derived read-only view of ``stage_status``;
        before persisting, force ``status = derive_status_from_stage(stage_status)``,
        and the status value passed by the caller is ignored (types.STAGE_TO_STATUS is the sole source of truth).

        Args:
            project_id: explicit scope; when empty use _project_scope, and when still empty use review.project_id.
        """
        effective_pid = self._effective_project_id(project_id) or review.project_id
        if effective_pid and not review.project_id:
            review.project_id = effective_pid
        review.status = derive_status_from_stage(review.stage_status)
        async with get_session(self._db_url) as session:
            session.add(EcosystemDeepReviewModel.from_pydantic(review))
        return review

    async def get_deep_review(
        self,
        review_id: str,
        project_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Fetch a deep-scan report by id.

        Args:
            project_id: explicit scope; when empty falls back to _project_scope; when the scope is non-empty, matches
            only the same project.
        """
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel).where(
                EcosystemDeepReviewModel.id == review_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_deep_review(
        self,
        review_id: str,
        **fields: Any,
    ) -> EcosystemDeepReview | None:
        """Partially update a deep-scan report's fields. Enums in fields are automatically converted to .value.

        - If fields contains the _project_id keyword (passed explicitly by the service), match only the same project;
        - otherwise, if the current repo has _project_scope set, only rows under the same project may be updated.
        """
        explicit_pid = fields.pop("_project_id", None)
        effective_pid = explicit_pid or self._effective_project_id(None)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel).where(
                EcosystemDeepReviewModel.id == review_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in fields.items():
                if hasattr(value, "value"):
                    value = value.value  # noqa: PLW2901  # enum -> str
                if hasattr(row, key):
                    setattr(row, key, value)
            await session.flush()
            return row.to_pydantic()

    async def list_deep_reviews(
        self,
        repo_id: str = "",
        status: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[EcosystemDeepReview]:
        """List deep-scan reports by repo_id or status. An empty string ignores the corresponding filter.

        Args:
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel)
            stmt = self._apply_project_filter(stmt, EcosystemDeepReviewModel)
            if project_id:
                stmt = stmt.where(
                    EcosystemDeepReviewModel.project_id == project_id
                )
            if repo_id:
                stmt = stmt.where(EcosystemDeepReviewModel.repo_id == repo_id)
            if status:
                stmt = stmt.where(EcosystemDeepReviewModel.status == status)
            stmt = stmt.order_by(EcosystemDeepReviewModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Ecosystem worker pool claim helpers (v1.5.3)
    # ================================================================

    @staticmethod
    def _claim_lease_available(
        now: datetime,
        ttl_seconds: int = STALE_CLAIM_TTL_SECONDS,
    ) -> Any:
        """SQL predicate for an available claim lease: unclaimed / age-indeterminable / expired.

        Args:
            now: the current moment (aware UTC, see aiteam/clock.py).
            ttl_seconds: the lease duration; beyond it the claim is treated as aborted and takeover-able.

        Returns:
            Can be attached directly to a select/update ``WHERE`` condition.
        """
        cutoff = now - timedelta(seconds=ttl_seconds)
        return or_(
            EcosystemDeepReviewModel.claimed_by.is_(None),
            EcosystemDeepReviewModel.claimed_at.is_(None),
            EcosystemDeepReviewModel.claimed_at < cutoff,
        )

    async def reclaim_stale_shallow_claims(
        self,
        *,
        dry_run: bool = True,
        ttl_seconds: int = STALE_CLAIM_TTL_SECONDS,
        project_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Reclaim (or only preview) lease-expired ``queued`` claim rows.

        After a claim lease expires, ``claim_next_shallow_repo`` can already take over by itself; this method is the
        companion
        **bulk ledger cleanup**: explicitly clear dead reservations so ``shallow_queue_status``'s
        pending / in_progress counts immediately return to the truth, and operations can stop the bleeding in one pass
        (see ``scripts/reclaim_stale_shallow_claims.py``).

        Args:
            dry_run: True (default) only returns the rows that would be reclaimed, without writing to the DB.
            ttl_seconds: the lease duration used to judge expiry.
            project_id: explicit scope; when empty, falls back to _project_scope.
            limit: the per-call processing cap, preventing a full-table scan in one go.

        Returns:
            Each row ``{id, repo_id, project_id, claimed_by, claimed_at, stale_seconds}``,
            sorted by claim time from oldest to newest. With dry_run=False, these rows' claims have been cleared.
        """
        effective_pid = self._effective_project_id(project_id)
        now = utc_now()
        cutoff = now - timedelta(seconds=ttl_seconds)

        async with get_session(self._db_url) as session:
            stmt = (
                select(EcosystemDeepReviewModel)
                .where(EcosystemDeepReviewModel.stage_status == "queued")
                .where(EcosystemDeepReviewModel.claimed_by.is_not(None))
                .where(
                    or_(
                        EcosystemDeepReviewModel.claimed_at.is_(None),
                        EcosystemDeepReviewModel.claimed_at < cutoff,
                    )
                )
                .order_by(EcosystemDeepReviewModel.claimed_at.asc())
                .limit(limit)
            )
            stmt = self._apply_project_filter(stmt, EcosystemDeepReviewModel)
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

            preview: list[dict[str, Any]] = [
                {
                    "id": row.id,
                    "repo_id": row.repo_id,
                    "project_id": row.project_id,
                    "claimed_by": row.claimed_by,
                    "claimed_at": (
                        row.claimed_at.isoformat() if row.claimed_at else None
                    ),
                    "stale_seconds": (
                        (now - row.claimed_at).total_seconds()
                        if row.claimed_at
                        else float("inf")
                    ),
                }
                for row in rows
            ]

            if not dry_run:
                for row in rows:
                    row.claimed_by = None
                    row.claimed_at = None
                await session.flush()

            return preview

    async def claim_next_shallow_repo(
        self,
        worker_id: str,
        project_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Atomically claim the next repo awaiting a shallow scan (stage_status='queued' and the claim lease available).

        Uses a two-step atomic protocol:
        1. UPDATE ... WHERE stage_status='queued' AND <lease available> LIMIT 1 SET claimed_by=worker_id
        2. Judge success by rowcount (rowcount > 0 = claim succeeded), then SELECT back the full row.

        SQLite's serialized writes guarantee no other writer interleaves between the two steps, so rowcount is
        the sole source of truth for "did I really update the row", avoiding SKIP LOCKED (unsupported by SQLite).

        "Lease available" = unclaimed, or the claim has expired (beyond ``STALE_CLAIM_TTL_SECONDS``,
        including age-indeterminable rows with a missing claimed_at). The expiry condition is the antidote to queue
        deadlock: when a tick creates a row, it
        atomically pre-writes claimed_by to prevent double-grabbing (D5), but once a dispatched sub-agent aborts,
        this reservation used to be **permanent** — the row stayed queued+claimed forever, invisible to all claimants
        (measured in production: 74 rows stuck dead for 17 days, pending constantly 0).

        Args:
            worker_id: the claimant's unique identifier string.
            project_id: explicit scope; when empty, falls back to _project_scope.

        Returns:
            The claimed deep-scan report (with claimed_by/claimed_at written); returns None when no row is available.
        """
        from sqlalchemy import text

        effective_pid = self._effective_project_id(project_id)
        now = utc_now()
        lease_free = self._claim_lease_available(now)

        async with get_session(self._db_url) as session:
            # Retry loop: handle the case where another worker claims the same candidate
            # between our SELECT (read) and UPDATE (write). SQLite serializes writes so
            # rowcount=0 means we lost the race; we then find the next unclaimed row.
            already_tried: set[str] = set()
            while True:
                # Step 1: find the next claimable candidate (skip already-tried ids)
                candidate_stmt = (
                    select(EcosystemDeepReviewModel.id)
                    .where(EcosystemDeepReviewModel.stage_status == "queued")
                    .where(lease_free)
                    .order_by(EcosystemDeepReviewModel.created_at.asc())
                    .limit(1)
                )
                if effective_pid is not None:
                    candidate_stmt = candidate_stmt.where(
                        EcosystemDeepReviewModel.project_id == effective_pid
                    )
                # Expire session cache so we see committed changes from other workers
                await session.execute(text("SELECT 1"))
                candidate_result = await session.execute(candidate_stmt)
                candidate_id = candidate_result.scalar_one_or_none()
                if candidate_id is None:
                    return None
                if candidate_id in already_tried:
                    return None  # Row exists but was just claimed by someone else
                already_tried.add(candidate_id)

                # Step 2: atomic UPDATE — wins only if the lease is still free.
                # Typed ORM UPDATE (not raw text) so ``claimed_at`` is bound through
                # SQLAlchemy's SQLite DATETIME processor: raw ``isoformat()`` writes
                # "…T…+00:00" while the ORM writes "… …", and the two do not compare
                # lexicographically — a mixed-format column would break TTL arithmetic.
                update_result = await session.execute(
                    sa_update(EcosystemDeepReviewModel)
                    .where(EcosystemDeepReviewModel.id == candidate_id)
                    .where(lease_free)
                    .values(claimed_by=worker_id, claimed_at=now)
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount > 0:
                    break  # We won the race — fetch and return

            # Step 3: fetch the updated row to return full Pydantic model
            await session.execute(text("SELECT 1"))  # flush read cache
            fetch_result = await session.execute(
                select(EcosystemDeepReviewModel).where(
                    EcosystemDeepReviewModel.id == candidate_id
                )
            )
            row = fetch_result.scalar_one_or_none()
            if row is None:
                return None
            await session.refresh(row)
            return row.to_pydantic()

    async def claim_next_review_repo(
        self,
        worker_id: str,
        project_id: str | None = None,
        min_stars: int = 0,
    ) -> tuple[EcosystemDeepReview, EcosystemRepoProfile] | None:
        """Atomically claim the next row awaiting quality review (stage_status='shallow_done', quality_score IS NULL,
        lease available).

        Also returns the associated EcosystemRepoProfile (including shallow_summary for the reviewer's assessment).
        Lease semantics from the same origin as the shallow-scan claim: a claim that does not converge within
        ``STALE_CLAIM_TTL_SECONDS``
        is treated as aborted and takeover-able, preventing a review worker going offline from locking the row forever.

        Args:
            worker_id: the claimant's unique identifier string.
            project_id: explicit scope; when empty, falls back to _project_scope.
            min_stars: minimum-stars filter; 0 means no filtering.

        Returns:
            A (deep_review, repo_profile) tuple; returns None when no row is available.
        """
        effective_pid = self._effective_project_id(project_id)
        now = utc_now()
        lease_free = self._claim_lease_available(now)

        async with get_session(self._db_url) as session:
            # Step 1: find candidate with JOIN on profile stars filter
            candidate_stmt = (
                select(EcosystemDeepReviewModel.id, EcosystemDeepReviewModel.repo_id)
                .join(
                    EcosystemRepoProfileModel,
                    EcosystemDeepReviewModel.repo_id == EcosystemRepoProfileModel.id,
                )
                .where(EcosystemDeepReviewModel.stage_status == "shallow_done")
                .where(EcosystemDeepReviewModel.quality_score.is_(None))
                .where(lease_free)
                .order_by(EcosystemDeepReviewModel.created_at.asc())
                .limit(1)
            )
            if effective_pid is not None:
                candidate_stmt = candidate_stmt.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            if min_stars > 0:
                candidate_stmt = candidate_stmt.where(
                    EcosystemRepoProfileModel.stars >= min_stars
                )
            candidate_result = await session.execute(candidate_stmt)
            candidate_row = candidate_result.first()
            if candidate_row is None:
                return None
            candidate_id, repo_id = candidate_row

            # Step 2: atomic UPDATE — wins only if the lease is still free
            update_result = await session.execute(
                sa_update(EcosystemDeepReviewModel)
                .where(EcosystemDeepReviewModel.id == candidate_id)
                .where(lease_free)
                .values(claimed_by=worker_id, claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            if update_result.rowcount == 0:
                return None

            # Step 3: fetch updated review + profile
            dr_result = await session.execute(
                select(EcosystemDeepReviewModel).where(
                    EcosystemDeepReviewModel.id == candidate_id
                )
            )
            dr_row = dr_result.scalar_one_or_none()
            if dr_row is None:
                return None
            await session.refresh(dr_row)

            profile_result = await session.execute(
                select(EcosystemRepoProfileModel).where(
                    EcosystemRepoProfileModel.id == repo_id
                )
            )
            profile_row = profile_result.scalar_one_or_none()
            if profile_row is None:
                return None
            return dr_row.to_pydantic(), profile_row.to_pydantic()

    async def apply_quality_review(
        self,
        dr_id: str,
        quality_score: int,
        quality_notes: str,
        recommendation: str,
        project_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Write the quality review result and release the claim lock.

        Writes quality_score / quality_notes / reviewed_by / reviewed_at,
        clears claimed_by to release the claim lock, and updates integration_recommendation.

        Args:
            dr_id: the target EcosystemDeepReview.id.
            quality_score: a 0-100 quality score.
            quality_notes: the review rationale text.
            recommendation: integrate / reference / learn / skip.
            project_id: explicit scope; when empty, falls back to _project_scope.

        Returns:
            The updated deep-scan report; returns None when the row does not exist.
        """
        effective_pid = self._effective_project_id(project_id)
        now = utc_now()

        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel).where(
                EcosystemDeepReviewModel.id == dr_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemDeepReviewModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.quality_score = quality_score
            row.quality_notes = quality_notes
            row.reviewed_by = row.claimed_by  # reviewer = who claimed it
            row.reviewed_at = now
            row.claimed_by = None  # release claim lock
            row.claimed_at = None
            if recommendation:
                row.integration_recommendation = recommendation
            await session.flush()
            return row.to_pydantic()

    async def release_claim(
        self,
        dr_id: str,
        reason: str,
        project_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Release the claim lock (without writing a quality score), recording reason into quality_notes.

        Used when a worker abandons a claim (timeout, error); clears claimed_by so other workers can claim again.

        Args:
            dr_id: the target EcosystemDeepReview.id.
            reason: the release reason, recorded into quality_notes.
            project_id: explicit scope; when empty, falls back to _project_scope.

        Returns:
            The updated deep-scan report; returns None when the row does not exist.
        """
        effective_pid = self._effective_project_id(project_id)

        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel).where(
                EcosystemDeepReviewModel.id == dr_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemDeepReviewModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.claimed_by = None
            row.claimed_at = None
            row.quality_notes = reason
            await session.flush()
            return row.to_pydantic()

    # ================================================================
    # Ecosystem tags
    # ================================================================

    async def upsert_tag(self, tag: EcosystemTag) -> EcosystemTag:
        """Upsert a tag by the name unique key. If it exists, update the non-primary-key fields."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemTagModel).where(EcosystemTagModel.name == tag.name)
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(EcosystemTagModel.from_pydantic(tag))
            else:
                row.aliases = tag.aliases
                row.category = tag.category.value
                row.description = tag.description
        return tag

    async def get_tag_by_name(self, name: str) -> EcosystemTag | None:
        """Fetch a tag by name."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemTagModel).where(EcosystemTagModel.name == name)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def get_tag(self, tag_id: str) -> EcosystemTag | None:
        """Fetch a tag by id."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemTagModel).where(EcosystemTagModel.id == tag_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_tags(
        self, category: str = "", limit: int = 200
    ) -> list[EcosystemTag]:
        """List tags, optionally filtered by category."""
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemTagModel)
            if category:
                stmt = stmt.where(EcosystemTagModel.category == category)
            stmt = stmt.order_by(EcosystemTagModel.name).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Ecosystem repo-tag associations
    # ================================================================

    async def add_repo_tag(
        self,
        repo_tag: EcosystemRepoTag,
        project_id: str | None = None,
    ) -> EcosystemRepoTag:
        """Add a repo-tag association; if it exists, update confidence/source/agent_id (application-layer upsert).

        Args:
            project_id: explicit scope; when empty, falls back to _project_scope/repo_tag.project_id.
        """
        effective_pid = self._effective_project_id(project_id) or repo_tag.project_id
        if effective_pid and not repo_tag.project_id:
            repo_tag.project_id = effective_pid

        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemRepoTagModel).where(
                    EcosystemRepoTagModel.repo_id == repo_tag.repo_id,
                    EcosystemRepoTagModel.tag_id == repo_tag.tag_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(EcosystemRepoTagModel.from_pydantic(repo_tag))
            else:
                row.confidence = repo_tag.confidence
                row.source = repo_tag.source.value
                row.agent_id = repo_tag.agent_id
                if effective_pid and not row.project_id:
                    row.project_id = effective_pid
        return repo_tag

    async def remove_repo_tag(
        self,
        repo_id: str,
        tag_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Remove a repo-tag association; returns whether a record was actually deleted."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoTagModel).where(
                EcosystemRepoTagModel.repo_id == repo_id,
                EcosystemRepoTagModel.tag_id == tag_id,
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemRepoTagModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            return True

    async def delete_repo_tags_by_sources(
        self,
        repo_id: str,
        sources: list[str],
        project_id: str | None = None,
    ) -> int:
        """Bulk-delete a repo's tag associations whose source is in ``sources``.

        Used by ``apply_batch(replace_auto=True)`` to clear stale auto-generated
        tags before re-applying. ``MANUAL`` and other sources are preserved.

        Args:
            repo_id: Target EcosystemRepoProfile.id.
            sources: List of source values to delete (e.g. ["github_topic", "auto_rule"]).
            project_id: Explicit project scope; falls back to repository scope.

        Returns:
            Number of rows actually deleted.
        """
        if not sources:
            return 0
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoTagModel).where(
                EcosystemRepoTagModel.repo_id == repo_id,
                EcosystemRepoTagModel.source.in_(sources),
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemRepoTagModel.project_id == effective_pid)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            for row in rows:
                await session.delete(row)
            return len(rows)

    async def list_repo_tags(
        self,
        repo_id: str = "",
        tag_id: str = "",
        limit: int = 200,
        project_id: str | None = None,
    ) -> list[EcosystemRepoTag]:
        """List repo-tag associations, filtered by repo_id or tag_id.

        Args:
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoTagModel)
            stmt = self._apply_project_filter(stmt, EcosystemRepoTagModel)
            if project_id:
                stmt = stmt.where(EcosystemRepoTagModel.project_id == project_id)
            if repo_id:
                stmt = stmt.where(EcosystemRepoTagModel.repo_id == repo_id)
            if tag_id:
                stmt = stmt.where(EcosystemRepoTagModel.tag_id == tag_id)
            stmt = stmt.order_by(EcosystemRepoTagModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Ecosystem relations (repo <-> repo)
    # ================================================================

    async def add_relation(
        self,
        relation: EcosystemRelation,
        project_id: str | None = None,
    ) -> EcosystemRelation:
        """Add a reference relation between repos.

        Args:
            project_id: explicit scope; when empty, falls back to _project_scope/relation.project_id.
        """
        effective_pid = self._effective_project_id(project_id) or relation.project_id
        if effective_pid and not relation.project_id:
            relation.project_id = effective_pid
        async with get_session(self._db_url) as session:
            session.add(EcosystemRelationModel.from_pydantic(relation))
        return relation

    async def list_relations(
        self,
        from_repo_id: str = "",
        to_repo_id: str = "",
        relation_type: str = "",
        limit: int = 200,
        project_id: str | None = None,
    ) -> list[EcosystemRelation]:
        """List repo-repo relations, filtered by source / target / type.

        Args:
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRelationModel)
            stmt = self._apply_project_filter(stmt, EcosystemRelationModel)
            if project_id:
                stmt = stmt.where(EcosystemRelationModel.project_id == project_id)
            if from_repo_id:
                stmt = stmt.where(EcosystemRelationModel.from_repo_id == from_repo_id)
            if to_repo_id:
                stmt = stmt.where(EcosystemRelationModel.to_repo_id == to_repo_id)
            if relation_type:
                stmt = stmt.where(EcosystemRelationModel.relation_type == relation_type)
            stmt = stmt.order_by(EcosystemRelationModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def remove_relation(
        self,
        relation_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Delete one relation record."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRelationModel).where(
                EcosystemRelationModel.id == relation_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemRelationModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            return True

    # ================================================================
    # Ecosystem scan runs
    # ================================================================

    async def create_scan_run(
        self,
        scan_run: EcosystemScanRun,
        project_id: str | None = None,
    ) -> EcosystemScanRun:
        """Create one scan batch record.

        Args:
            project_id: explicit scope; when empty, falls back to _project_scope / scan_run.project_id.
        """
        effective_pid = self._effective_project_id(project_id) or scan_run.project_id
        if effective_pid and not scan_run.project_id:
            scan_run.project_id = effective_pid
        async with get_session(self._db_url) as session:
            session.add(EcosystemScanRunModel.from_pydantic(scan_run))
        return scan_run

    async def get_scan_run(
        self,
        scan_run_id: str,
        project_id: str | None = None,
    ) -> EcosystemScanRun | None:
        """Fetch a scan batch by id."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemScanRunModel).where(
                EcosystemScanRunModel.id == scan_run_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemScanRunModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_scan_run(
        self,
        scan_run_id: str,
        **fields: Any,
    ) -> EcosystemScanRun | None:
        """Partially update a scan batch's fields. Enums in fields are automatically converted to .value.

        When the scope is non-empty, only rows under the same project may be updated.
        """
        effective_pid = self._effective_project_id(None)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemScanRunModel).where(
                EcosystemScanRunModel.id == scan_run_id
            )
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemScanRunModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in fields.items():
                if hasattr(value, "value"):
                    value = value.value  # noqa: PLW2901
                if hasattr(row, key):
                    setattr(row, key, value)
            await session.flush()
            return row.to_pydantic()

    async def list_scan_runs(
        self,
        strategy: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[EcosystemScanRun]:
        """List scan batches by strategy, in started_at descending order.

        Args:
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemScanRunModel)
            stmt = self._apply_project_filter(stmt, EcosystemScanRunModel)
            if project_id:
                stmt = stmt.where(EcosystemScanRunModel.project_id == project_id)
            if strategy:
                stmt = stmt.where(EcosystemScanRunModel.strategy == strategy)
            stmt = stmt.order_by(EcosystemScanRunModel.started_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Stage E: Ecosystem extended search & holistic detail
    # ================================================================

    async def search_ecosystem_profiles_extended(
        self,
        keyword: str = "",
        topic: str = "",
        min_stars: int = 0,
        max_stars: int | None = None,
        needs_deep_review: bool | None = None,
        category: str = "",
        language: str = "",
        pushed_after: datetime | None = None,
        is_archived: bool | None = None,
        tags: list[str] | None = None,
        tag_match_mode: str = "all",
        sort: str = "stars",
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
        id_filter: list[str] | None = None,
        id_exclude: list[str] | None = None,
    ) -> tuple[list[EcosystemRepoProfile], int]:
        """Stage E extended search: supports tags / language / pushed_after / multiple sort orders.

        Parameters:
            tags: a list of tag names (matched against EcosystemTag.name). An empty list / None means no filtering.
            tag_match_mode: "all" (default, AND semantics, all tags must exist) / "any" (OR semantics).
            sort: "stars" (default, stars desc) / "recency" (pushed_at desc, nulls last)
                / "relevance" (relevance_score desc).
            offset: the pagination offset, used with limit for paging.

        Returns (profiles, total_count_before_limit).
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel)
            stmt = self._apply_project_filter(stmt, EcosystemRepoProfileModel)
            if project_id:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.project_id == project_id
                )

            # Basic field filtering
            if id_filter is not None:
                if not id_filter:
                    return ([], 0)
                stmt = stmt.where(EcosystemRepoProfileModel.id.in_(id_filter))
            if id_exclude:
                stmt = stmt.where(EcosystemRepoProfileModel.id.notin_(id_exclude))
            if min_stars > 0:
                stmt = stmt.where(EcosystemRepoProfileModel.stars >= min_stars)
            if max_stars is not None and max_stars > 0:
                stmt = stmt.where(EcosystemRepoProfileModel.stars <= max_stars)
            if needs_deep_review is not None:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.needs_deep_review == needs_deep_review
                )
            if category:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.relevance_category == category
                )
            if language:
                stmt = stmt.where(EcosystemRepoProfileModel.language == language)
            if pushed_after is not None:
                stmt = stmt.where(EcosystemRepoProfileModel.pushed_at >= pushed_after)
            if is_archived is not None:
                stmt = stmt.where(
                    EcosystemRepoProfileModel.is_archived == is_archived
                )
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        EcosystemRepoProfileModel.name.ilike(kw),
                        EcosystemRepoProfileModel.description.ilike(kw),
                        EcosystemRepoProfileModel.one_line_summary.ilike(kw),
                        EcosystemRepoProfileModel.repo_full_name.ilike(kw),
                        EcosystemRepoProfileModel.description_excerpt.ilike(kw),
                    )
                )
            if topic:
                tp = f"%{topic}%"
                stmt = stmt.where(EcosystemRepoProfileModel.topics.ilike(tp))

            # tags filtering: AND/OR implemented via EXISTS subqueries
            if tags:
                # Resolve tag names -> tag_ids
                tag_ids: list[str] = []
                tag_rows = await session.execute(
                    select(EcosystemTagModel).where(EcosystemTagModel.name.in_(tags))
                )
                for t in tag_rows.scalars().all():
                    tag_ids.append(t.id)

                if not tag_ids:
                    # No tags exist at all -> 0 results
                    return ([], 0)

                if tag_match_mode == "any":
                    # OR semantics: the repo has at least one matching tag
                    subq = (
                        select(EcosystemRepoTagModel.repo_id)
                        .where(EcosystemRepoTagModel.tag_id.in_(tag_ids))
                    )
                    stmt = stmt.where(EcosystemRepoProfileModel.id.in_(subq))
                else:
                    # AND semantics: every tag_id must have a corresponding repo_tag row
                    for tid in tag_ids:
                        subq = (
                            select(EcosystemRepoTagModel.repo_id)
                            .where(EcosystemRepoTagModel.tag_id == tid)
                        )
                        stmt = stmt.where(EcosystemRepoProfileModel.id.in_(subq))

            # Compute the total (before offset/limit)
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total_result = await session.execute(count_stmt)
            total = int(total_result.scalar() or 0)

            # Sorting
            if sort == "recency":
                # SQLite-compatible NULLS LAST approach: is null asc + value desc
                stmt = stmt.order_by(
                    EcosystemRepoProfileModel.pushed_at.is_(None).asc(),
                    EcosystemRepoProfileModel.pushed_at.desc(),
                    EcosystemRepoProfileModel.stars.desc(),
                )
            elif sort == "relevance":
                stmt = stmt.order_by(
                    EcosystemRepoProfileModel.relevance_score.desc(),
                    EcosystemRepoProfileModel.stars.desc(),
                )
            else:  # default "stars"
                stmt = stmt.order_by(EcosystemRepoProfileModel.stars.desc())

            stmt = stmt.offset(max(offset, 0)).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return ([r.to_pydantic() for r in rows], total)

    async def compute_ecosystem_facet_counts(
        self,
        keyword: str = "",
        min_stars: int = 0,
        max_stars: int | None = None,
        category: str = "",
        language: str = "",
        is_archived: bool | None = None,
        project_id: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Compute facet aggregations: under the given base filters, count the distribution of category / language /
        archived.

        Returns {"category": {...}, "language": {...}, "archived": {"true": n, "false": n}}.

        K1 perf: the three GROUP BYs share one session + a single ``_apply_filters`` helper,
        eliminating the original's cat_stmt dead code and 5 duplicated ``or_(...)`` segments. GROUP BY aggregation at
        the SQL layer
        is faster than fetching all rows and aggregating in Python on large tables (measured: 50K rows, GROUP BY 132ms
        vs Python full fetch 152ms).
        """
        from sqlalchemy import or_

        effective_pid = self._effective_project_id(project_id)

        def _apply_filters(q):
            """Apply common filters (project + stars + cat/lang/archived + keyword)."""
            if effective_pid is not None:
                q = q.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            if min_stars > 0:
                q = q.where(EcosystemRepoProfileModel.stars >= min_stars)
            if max_stars is not None and max_stars > 0:
                q = q.where(EcosystemRepoProfileModel.stars <= max_stars)
            if category:
                q = q.where(
                    EcosystemRepoProfileModel.relevance_category == category
                )
            if language:
                q = q.where(EcosystemRepoProfileModel.language == language)
            if is_archived is not None:
                q = q.where(
                    EcosystemRepoProfileModel.is_archived == is_archived
                )
            if keyword:
                kw = f"%{keyword}%"
                q = q.where(
                    or_(
                        EcosystemRepoProfileModel.name.ilike(kw),
                        EcosystemRepoProfileModel.description.ilike(kw),
                        EcosystemRepoProfileModel.one_line_summary.ilike(kw),
                        EcosystemRepoProfileModel.repo_full_name.ilike(kw),
                        EcosystemRepoProfileModel.description_excerpt.ilike(kw),
                    )
                )
            return q

        async with get_session(self._db_url) as session:
            cat_q = _apply_filters(
                select(
                    EcosystemRepoProfileModel.relevance_category,
                    func.count(EcosystemRepoProfileModel.id).label("cnt"),
                )
            ).group_by(EcosystemRepoProfileModel.relevance_category)
            cat_result = await session.execute(cat_q)
            category_counts: dict[str, int] = {}
            for row in cat_result.all():
                key = row[0] or "unknown"
                category_counts[key] = int(row[1])

            lang_q = _apply_filters(
                select(
                    EcosystemRepoProfileModel.language,
                    func.count(EcosystemRepoProfileModel.id).label("cnt"),
                )
            ).group_by(EcosystemRepoProfileModel.language)
            lang_result = await session.execute(lang_q)
            language_counts: dict[str, int] = {}
            for row in lang_result.all():
                key = row[0] or "unknown"
                language_counts[key] = int(row[1])

            arch_q = _apply_filters(
                select(
                    EcosystemRepoProfileModel.is_archived,
                    func.count(EcosystemRepoProfileModel.id).label("cnt"),
                )
            ).group_by(EcosystemRepoProfileModel.is_archived)
            arch_result = await session.execute(arch_q)
            archived_counts: dict[str, int] = {"true": 0, "false": 0}
            for row in arch_result.all():
                key = "true" if row[0] else "false"
                archived_counts[key] = int(row[1])

            # v1.5.1: stage facet — exposes the progressive funnel's state distribution (so the frontend
            # StatsBar/filters are not truncated by limit)
            # Implementation: 1) take all deep_reviews in created_at desc order and aggregate a latest stage map on the
            # Python side
            #       2) then take all profile ids satisfying the filter and aggregate by stage_map
            # At the current data scale (< 10k reviews) this approach suffices; larger tables can later switch to
            # window-function SQL.
            all_reviews_q = select(
                EcosystemDeepReviewModel.repo_id,
                EcosystemDeepReviewModel.stage_status,
            ).order_by(EcosystemDeepReviewModel.created_at.desc())
            if effective_pid is not None:
                all_reviews_q = all_reviews_q.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            all_reviews_result = await session.execute(all_reviews_q)
            latest_stage_map: dict[str, str] = {}
            for row in all_reviews_result.all():
                rid = row[0]
                if rid in latest_stage_map:
                    continue
                stg = row[1]
                latest_stage_map[rid] = (
                    stg.value if hasattr(stg, "value") else stg
                ) or "queued"

            profile_ids_q = _apply_filters(select(EcosystemRepoProfileModel.id))
            profile_ids_result = await session.execute(profile_ids_q)
            stage_counts: dict[str, int] = {}
            for row in profile_ids_result.all():
                stg2 = latest_stage_map.get(row[0], "queued")
                stage_counts[stg2] = stage_counts.get(stg2, 0) + 1

            # v1.6.0 SST: topics facet — full project (no filter beyond project_id)
            # topics stored as JSON string; parse and count per-topic across all project repos
            topics_q = select(EcosystemRepoProfileModel.topics)
            if effective_pid is not None:
                topics_q = topics_q.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            topics_result = await session.execute(topics_q)
            topics_counter: dict[str, int] = {}
            import json as _json
            for (topics_raw,) in topics_result.all():
                if not topics_raw:
                    continue
                try:
                    topic_list = _json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
                    if isinstance(topic_list, list):
                        for t in topic_list:
                            if isinstance(t, str) and t:
                                topics_counter[t] = topics_counter.get(t, 0) + 1
                except Exception:
                    pass
            topics_sorted = dict(sorted(topics_counter.items(), key=lambda x: -x[1]))

            return {
                "category": category_counts,
                "language": language_counts,
                "archived": archived_counts,
                "stage": stage_counts,
                "topics": topics_sorted,
            }

    async def get_ecosystem_profile_full(
        self,
        repo_full_name: str = "",
        repo_id: str = "",
        relations_limit: int = 50,
        deep_reviews_limit: int = 20,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a repo profile holographically: profile + tags (with names) + deep_reviews + relations (from/to) +
        scan_run.

        repo_full_name and repo_id are alternatives (repo_id takes priority).
        Returns None when the repo does not exist.

        Args:
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        # 1) main profile
        profile: EcosystemRepoProfile | None = None
        if repo_id:
            profile = await self.get_ecosystem_profile_by_id(
                repo_id, project_id=project_id
            )
        elif repo_full_name:
            profile = await self.get_ecosystem_profile(
                repo_full_name, project_id=project_id
            )
        if profile is None:
            return None

        # 2) tags (join EcosystemTag for name + category)
        async with get_session(self._db_url) as session:
            tag_join = await session.execute(
                select(EcosystemRepoTagModel, EcosystemTagModel)
                .join(
                    EcosystemTagModel,
                    EcosystemTagModel.id == EcosystemRepoTagModel.tag_id,
                )
                .where(EcosystemRepoTagModel.repo_id == profile.id)
                .order_by(EcosystemTagModel.name)
            )
            tags_payload: list[dict[str, Any]] = []
            for repo_tag, tag in tag_join.all():
                tags_payload.append(
                    {
                        "tag_id": tag.id,
                        "name": tag.name,
                        "category": tag.category,
                        "aliases": tag.aliases or [],
                        "description": tag.description,
                        "confidence": repo_tag.confidence,
                        "source": repo_tag.source,
                        "agent_id": repo_tag.agent_id,
                        "created_at": (
                            repo_tag.created_at.isoformat()
                            if repo_tag.created_at
                            else None
                        ),
                    }
                )

        # 3) deep reviews (with project scope)
        deep_reviews = await self.list_deep_reviews(
            repo_id=profile.id,
            limit=deep_reviews_limit,
            project_id=project_id,
        )

        # 4) relations from + to (including brief info on the target repo)
        async with get_session(self._db_url) as session:
            from_rows = await session.execute(
                select(EcosystemRelationModel, EcosystemRepoProfileModel)
                .outerjoin(
                    EcosystemRepoProfileModel,
                    EcosystemRepoProfileModel.id
                    == EcosystemRelationModel.to_repo_id,
                )
                .where(EcosystemRelationModel.from_repo_id == profile.id)
                .order_by(EcosystemRelationModel.created_at.desc())
                .limit(relations_limit)
            )
            relations_from: list[dict[str, Any]] = []
            for rel, target in from_rows.all():
                relations_from.append(
                    {
                        "relation_id": rel.id,
                        "relation_type": rel.relation_type,
                        "to_repo_id": rel.to_repo_id,
                        "to_repo_full_name": (
                            target.repo_full_name if target else None
                        ),
                        "to_repo_stars": target.stars if target else None,
                        "evidence": rel.evidence,
                        "confidence": rel.confidence,
                        "agent_id": rel.agent_id,
                    }
                )

            to_rows = await session.execute(
                select(EcosystemRelationModel, EcosystemRepoProfileModel)
                .outerjoin(
                    EcosystemRepoProfileModel,
                    EcosystemRepoProfileModel.id
                    == EcosystemRelationModel.from_repo_id,
                )
                .where(EcosystemRelationModel.to_repo_id == profile.id)
                .order_by(EcosystemRelationModel.created_at.desc())
                .limit(relations_limit)
            )
            relations_to: list[dict[str, Any]] = []
            for rel, source in to_rows.all():
                relations_to.append(
                    {
                        "relation_id": rel.id,
                        "relation_type": rel.relation_type,
                        "from_repo_id": rel.from_repo_id,
                        "from_repo_full_name": (
                            source.repo_full_name if source else None
                        ),
                        "from_repo_stars": source.stars if source else None,
                        "evidence": rel.evidence,
                        "confidence": rel.confidence,
                        "agent_id": rel.agent_id,
                    }
                )

        # 5) scan_run (with project scope)
        scan_run = None
        if profile.scan_run_id:
            scan_run = await self.get_scan_run(
                profile.scan_run_id, project_id=project_id
            )

        return {
            "profile": profile,
            "tags": tags_payload,
            "deep_reviews": deep_reviews,
            "relations_from": relations_from,
            "relations_to": relations_to,
            "scan_run": scan_run,
        }

    # ================================================================
    # Stage J: project-isolation backfill
    # ================================================================

    async def backfill_ecosystem_to_project(
        self, project_id: str
    ) -> dict[str, int]:
        """Migrate all ecosystem data with project_id IS NULL to the specified project.

        Covers 5 data tables: profiles / scan_runs / deep_reviews / repo_tags / relations.
        The EcosystemTag dictionary stays NULL (shared globally).

        Idempotent: rows that already have a project_id are untouched; only NULL rows are migrated.

        Args:
            project_id: the target project's id (must exist in the projects table).

        Returns:
            A dict of the rows actually migrated per table.
        """
        from sqlalchemy import update

        if not project_id:
            raise ValueError("project_id must not be empty")

        async with get_session(self._db_url) as session:
            # verify the target project exists
            existing_project = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            if existing_project.scalar_one_or_none() is None:
                raise ValueError(f"Project {project_id} not found")

            counts: dict[str, int] = {}
            tables = [
                ("ecosystem_repo_profiles", EcosystemRepoProfileModel),
                ("ecosystem_scan_runs", EcosystemScanRunModel),
                ("ecosystem_deep_reviews", EcosystemDeepReviewModel),
                ("ecosystem_repo_tags", EcosystemRepoTagModel),
                ("ecosystem_relations", EcosystemRelationModel),
            ]
            for label, model in tables:
                stmt = (
                    update(model)
                    .where(model.project_id.is_(None))
                    .values(project_id=project_id)
                )
                result = await session.execute(stmt)
                counts[label] = int(result.rowcount or 0)

            return counts

    # ================================================================
    # v1.5.0-A: Stage status helpers (DeepReview progressive funnel)
    # ================================================================

    async def update_deep_review_stage(
        self,
        review_id: str,
        stage_status: EcosystemStageStatus | str,
        *,
        completed_at: datetime | None = None,
        debate_meeting_id: str | None = None,
        integration_task_id: str | None = None,
        integration_md: str | None = None,
        project_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Advance a DeepReview's stage_status, automatically writing the corresponding stage timestamp.

        D5 convergence choke point 2: every stage advance synchronously —
        - sets ``status`` := derive_status_from_stage(stage) (derived read-only view; the sole legitimate write entry);
        - uniformly clears ``claimed_by`` / ``claimed_at`` (releasing the claim, covering all dispatch sources: tick /
        lifecycle /
          claim workers, consistent with apply_quality_review / release_claim semantics);
        - when the derived state ∈ {completed, failed} and the row's ``completed_at`` is empty, backfills it (history is
        not rewritable).

        Args:
            review_id: the target DeepReview id.
            stage_status: the new stage value; accepts an enum or a string.
            completed_at: the stage completion time, defaulting to the current UTC time; specified explicitly when
            backfilling history.
            debate_meeting_id: the associated meeting id when stage_status=DEBATED.
            integration_task_id: the associated task id when stage_status=INTEGRATED.
            integration_md: the markdown content of the integration suggestion.
            project_id: explicit scope; when empty, _project_scope injects it automatically.

        Returns:
            The updated DeepReview Pydantic model; returns None when not found.
        """
        if isinstance(stage_status, str):
            stage_status = EcosystemStageStatus(stage_status)

        ts = completed_at or utc_now()

        # Different stages write different timestamp fields
        timestamp_fields: dict[str, datetime | None] = {}
        if stage_status == EcosystemStageStatus.SHALLOW_DONE:
            timestamp_fields["shallow_completed_at"] = ts
        elif stage_status == EcosystemStageStatus.ARCHITECTURE_DONE:
            timestamp_fields["architecture_completed_at"] = ts
        elif stage_status == EcosystemStageStatus.DEBATED:
            timestamp_fields["debated_at"] = ts
        elif stage_status in (
            EcosystemStageStatus.REFERENCED,
            EcosystemStageStatus.INTEGRATED,
        ):
            timestamp_fields["stage3_completed_at"] = ts

        derived = derive_status_from_stage(stage_status)
        update_kwargs: dict[str, Any] = {
            "stage_status": stage_status.value,
            # D5: the status column is a derived read-only view, linked to stage advancement only here.
            "status": derived.value,
            # D5: every stage advance/failure uniformly releases the claim, preventing dangling occupation.
            "claimed_by": None,
            "claimed_at": None,
        }
        update_kwargs.update(timestamp_fields)
        if debate_meeting_id is not None:
            update_kwargs["debate_meeting_id"] = debate_meeting_id
        if integration_task_id is not None:
            update_kwargs["integration_task_id"] = integration_task_id
        if integration_md is not None:
            update_kwargs["integration_md"] = integration_md
        if project_id is not None:
            update_kwargs["_project_id"] = project_id

        # A derived terminal state backfills completed_at (COALESCE semantics: an existing value is never overwritten,
        # preserving the historical snapshot)
        if derived in (
            EcosystemDeepReviewStatus.COMPLETED,
            EcosystemDeepReviewStatus.FAILED,
        ):
            current = await self.get_deep_review(review_id, project_id=project_id)
            if current is not None and current.completed_at is None:
                update_kwargs["completed_at"] = ts

        return await self.update_deep_review(review_id, **update_kwargs)

    async def list_deep_reviews_by_stage(
        self,
        stage_status: EcosystemStageStatus | str,
        repo_id: str = "",
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[EcosystemDeepReview]:
        """List deep reviews by stage_status.

        Used by the v1.5.0 funnel UI/Workers: e.g. query all repos at ``shallow_done`` for Stage 1
        candidate display, or query ``shallow_failed`` repos for the manual retry list.
        """
        if isinstance(stage_status, EcosystemStageStatus):
            stage_status = stage_status.value
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel)
            stmt = self._apply_project_filter(stmt, EcosystemDeepReviewModel)
            if project_id:
                stmt = stmt.where(EcosystemDeepReviewModel.project_id == project_id)
            stmt = stmt.where(EcosystemDeepReviewModel.stage_status == stage_status)
            if repo_id:
                stmt = stmt.where(EcosystemDeepReviewModel.repo_id == repo_id)
            stmt = stmt.order_by(EcosystemDeepReviewModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def count_shallow_dr_stages(
        self,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """Count the exact per-stage_status counts of shallow-scan DR rows.

        Bug 4 fix helper method: queries the ecosystem_deep_reviews table directly,
        avoiding the semantic skew caused by judging from an empty profile.shallow_summary.

        Returns:
            {
              "pending":     the number of rows that are queued with claimed_by IS NULL,
              "in_progress": the number of rows that are queued with claimed_by IS NOT NULL,
              "done":        the number of shallow_done rows,
              "failed":      the number of shallow_failed rows,
            }
        """
        from sqlalchemy import case, func

        effective_pid = self._effective_project_id(project_id)

        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    func.sum(
                        case(
                            (
                                (EcosystemDeepReviewModel.stage_status == "queued")
                                & EcosystemDeepReviewModel.claimed_by.is_(None),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("pending"),
                    func.sum(
                        case(
                            (
                                (EcosystemDeepReviewModel.stage_status == "queued")
                                & EcosystemDeepReviewModel.claimed_by.is_not(None),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("in_progress"),
                    func.sum(
                        case(
                            (EcosystemDeepReviewModel.stage_status == "shallow_done", 1),
                            else_=0,
                        )
                    ).label("done"),
                    func.sum(
                        case(
                            (EcosystemDeepReviewModel.stage_status == "shallow_failed", 1),
                            else_=0,
                        )
                    ).label("failed"),
                )
                .select_from(EcosystemDeepReviewModel)
            )
            stmt = self._apply_project_filter(stmt, EcosystemDeepReviewModel)
            if effective_pid is not None:
                stmt = stmt.where(
                    EcosystemDeepReviewModel.project_id == effective_pid
                )
            result = await session.execute(stmt)
            row = result.one_or_none()
            if row is None:
                return {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
            return {
                "pending": int(row.pending or 0),
                "in_progress": int(row.in_progress or 0),
                "done": int(row.done or 0),
                "failed": int(row.failed or 0),
            }

    async def backfill_shallow_done_status_completed(
        self,
        project_id: str | None = None,
    ) -> int:
        """v1.6.1 backfill: change the status of rows with stage_status='shallow_done' and status='running' to
        'completed'.

        Root cause: apply_shallow_summary's v1.5.2 comment mistakenly preserved status='running',
        leaving the status field of 678 shallow_done rows as dirty data, which made _dispatch_one's
        old status judgment wrongly skip all legacy-DB rescans. This backfill idempotently corrects the historical data.

        Returns:
            The number of rows actually updated.
        """
        from sqlalchemy import update as sa_update

        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = (
                sa_update(EcosystemDeepReviewModel)
                .where(EcosystemDeepReviewModel.stage_status == "shallow_done")
                .where(EcosystemDeepReviewModel.status == "running")
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemDeepReviewModel.project_id == effective_pid)
            stmt = stmt.values(status="completed")
            result = await session.execute(stmt)
            await session.flush()
            return result.rowcount or 0

    async def backfill_deep_review_dual_axis(
        self,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """D5 dual-axis convergence backfill: fully align the status derived view to the stage_status authoritative
        axis.

        5 pure UPDATEs executed serially in one pass in the fixed order R1→R2→F1→F2→F3 (the reverse rules must
        precede the forward rules, otherwise F3 would wrongly reset legacy completed rows left by old reviewers back to
        queued). The derivation mapping's sole source of truth is types.STAGE_TO_STATUS; this method's SQL
        literals are generated from that dict. UPDATEs only throughout (the append-only iron rule), idempotent:
        each WHERE excludes its own post-write state, so re-running always yields rowcount 0.

        Rules:
        - R1 reverse: (status=completed, stage=queued) completed rows from old reviewers →
          stage=shallow_done, shallow_completed_at=COALESCE(itself, completed_at, created_at).
          Eliminates the M20(a) "permanently skipped" residual root.
        - R2 reverse: (status=failed, stage=queued) with dispatch evidence
          (any of started_at / report_id / dispatch_prompt non-empty) → stage=shallow_failed.
          (failed, queued) rows without evidence are handed to F3 to reset to genuinely queued.
        - F1 forward: stage ∈ a completed state and status≠completed → status=completed,
          completed_at=COALESCE(itself, created_at). Covers the 678-row main lesion of running mistakenly left by v1.5.2
          and the pure-stage write rows of Stage1/2/3.
        - F2 forward: stage ∈ a failed state and status≠failed → status=failed, backfilling the timestamp as above.
        - F3 forward wrap-up (must be last): stage=queued and status≠queued → status=queued.

        Returns:
            A per-rule rowcount dict {"R1": n, "R2": n, "F1": n, "F2": n, "F3": n}.
        """
        from sqlalchemy import func, or_
        from sqlalchemy import update as sa_update

        queued_stage = EcosystemStageStatus.QUEUED.value
        completed_stages = sorted(
            s.value
            for s, v in STAGE_TO_STATUS.items()
            if v is EcosystemDeepReviewStatus.COMPLETED
        )
        failed_stages = sorted(
            s.value
            for s, v in STAGE_TO_STATUS.items()
            if v is EcosystemDeepReviewStatus.FAILED
        )

        effective_pid = self._effective_project_id(project_id)
        model = EcosystemDeepReviewModel

        def _scoped(stmt):  # noqa: ANN001, ANN202 — sqlalchemy Update
            if effective_pid is not None:
                return stmt.where(model.project_id == effective_pid)
            return stmt

        counts: dict[str, int] = {}
        async with get_session(self._db_url) as session:
            # R1 — reverse: push old reviewer rows stuck at completed+queued to shallow_done
            r1 = _scoped(
                sa_update(model)
                .where(model.stage_status == queued_stage)
                .where(model.status == EcosystemDeepReviewStatus.COMPLETED.value)
            ).values(
                stage_status=EcosystemStageStatus.SHALLOW_DONE.value,
                shallow_completed_at=func.coalesce(
                    model.shallow_completed_at,
                    model.completed_at,
                    model.created_at,
                ),
            )
            counts["R1"] = int((await session.execute(r1)).rowcount or 0)

            # R2 — reverse: push rows stuck at failed+queued with dispatch evidence to shallow_failed
            r2 = _scoped(
                sa_update(model)
                .where(model.stage_status == queued_stage)
                .where(model.status == EcosystemDeepReviewStatus.FAILED.value)
                .where(
                    or_(
                        model.started_at.is_not(None),
                        func.coalesce(model.report_id, "") != "",
                        func.coalesce(model.dispatch_prompt, "") != "",
                    )
                )
            ).values(stage_status=EcosystemStageStatus.SHALLOW_FAILED.value)
            counts["R2"] = int((await session.execute(r2)).rowcount or 0)

            # F1 — forward: align the status of completed-state stage rows to completed
            f1 = _scoped(
                sa_update(model)
                .where(model.stage_status.in_(completed_stages))
                .where(model.status != EcosystemDeepReviewStatus.COMPLETED.value)
            ).values(
                status=EcosystemDeepReviewStatus.COMPLETED.value,
                completed_at=func.coalesce(model.completed_at, model.created_at),
            )
            counts["F1"] = int((await session.execute(f1)).rowcount or 0)

            # F2 — forward: align the status of failed-state stage rows to failed
            f2 = _scoped(
                sa_update(model)
                .where(model.stage_status.in_(failed_stages))
                .where(model.status != EcosystemDeepReviewStatus.FAILED.value)
            ).values(
                status=EcosystemDeepReviewStatus.FAILED.value,
                completed_at=func.coalesce(model.completed_at, model.created_at),
            )
            counts["F2"] = int((await session.execute(f2)).rowcount or 0)

            # F3 — forward wrap-up (must be last): reset genuinely queued rows' status back to queued
            f3 = _scoped(
                sa_update(model)
                .where(model.stage_status == queued_stage)
                .where(model.status != EcosystemDeepReviewStatus.QUEUED.value)
            ).values(status=EcosystemDeepReviewStatus.QUEUED.value)
            counts["F3"] = int((await session.execute(f3)).rowcount or 0)

            await session.flush()
        return counts

    # ================================================================
    # v1.5.0-A: Failed flag helpers (Profile failure tracking)
    # ================================================================

    async def mark_profile_deleted(
        self,
        repo_id: str,
        *,
        error_message: str = "",
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Mark a profile as deleted on the GitHub side.

        - Sets is_deleted=True / is_active=False;
        - failure count +1, recording the latest error message;
        - the lifecycle tag is added separately by the caller via add_repo_tag('deleted', source='lifecycle').
        """
        return await self._update_profile_failure_state(
            repo_id,
            is_deleted=True,
            is_active=False,
            last_fetch_error=error_message or "GitHub 404",
            project_id=project_id,
        )

    async def mark_profile_private(
        self,
        repo_id: str,
        *,
        error_message: str = "",
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Mark a profile as made private on the GitHub side (403 forbidden, not rate limit)."""
        return await self._update_profile_failure_state(
            repo_id,
            is_private_now=True,
            is_active=False,
            last_fetch_error=error_message or "GitHub 403 forbidden",
            project_id=project_id,
        )

    async def mark_profile_fetch_failure(
        self,
        repo_id: str,
        *,
        error_message: str,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Record one fetch failure, failure_count +1.

        Does not toggle is_deleted/is_private/is_active; the caller decides by error type whether to escalate to
        mark_profile_deleted/private.
        """
        return await self._update_profile_failure_state(
            repo_id,
            last_fetch_error=error_message,
            project_id=project_id,
            increment_failure=True,
        )

    async def clear_profile_failure(
        self,
        repo_id: str,
        *,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Revival: the fetch succeeds again; clear the failure state (decision §3.3 revival mechanism).

        - is_deleted/is_private_now → False
        - is_active → True
        - last_fetch_error → ''
        - fetch_failure_count → 0
        """
        return await self._update_profile_failure_state(
            repo_id,
            is_deleted=False,
            is_private_now=False,
            is_active=True,
            last_fetch_error="",
            fetch_failure_count_reset=True,
            project_id=project_id,
        )

    async def _update_profile_failure_state(
        self,
        repo_id: str,
        *,
        is_deleted: bool | None = None,
        is_private_now: bool | None = None,
        is_active: bool | None = None,
        last_fetch_error: str | None = None,
        increment_failure: bool = False,
        fetch_failure_count_reset: bool = False,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Internal unified entry point; updates a profile's failure/activity-related fields."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.id == repo_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if is_deleted is not None:
                row.is_deleted = is_deleted
            if is_private_now is not None:
                row.is_private_now = is_private_now
            if is_active is not None:
                row.is_active = is_active
            if last_fetch_error is not None:
                row.last_fetch_error = last_fetch_error
            if increment_failure:
                row.fetch_failure_count = (row.fetch_failure_count or 0) + 1
            if fetch_failure_count_reset:
                row.fetch_failure_count = 0
            await session.flush()
            return row.to_pydantic()

    async def update_profile_active_set(
        self,
        repo_id: str,
        *,
        is_active: bool,
        active_rank: int | None,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Update a repo's active-set state (used internally by recompute_active_set)."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.id == repo_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.is_active = is_active
            row.active_rank = active_rank
            await session.flush()
            return row.to_pydantic()

    async def update_profile_shallow_summary(
        self,
        repo_id: str,
        *,
        shallow_summary: str,
        refreshed_at: datetime | None = None,
        project_id: str | None = None,
    ) -> EcosystemRepoProfile | None:
        """Write the Stage 0 shallow-scan summary, updating last_shallow_refreshed_at."""
        effective_pid = self._effective_project_id(project_id)
        ts = refreshed_at or utc_now()
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.id == repo_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.shallow_summary = shallow_summary or ""
            row.last_shallow_refreshed_at = ts
            await session.flush()
            return row.to_pydantic()

    # ================================================================
    # v1.5.0-A: Status snapshot helpers (append-only decision D)
    # ================================================================

    async def create_status_snapshot(
        self,
        snapshot: EcosystemRepoStatusSnapshot,
        project_id: str | None = None,
    ) -> EcosystemRepoStatusSnapshot:
        """Write one status snapshot (append-only).

        Args:
            snapshot: the status snapshot Pydantic model.
            project_id: explicit scope; when empty, falls back to _project_scope/snapshot.project_id.
        """
        effective_pid = self._effective_project_id(project_id) or snapshot.project_id
        if effective_pid and not snapshot.project_id:
            snapshot.project_id = effective_pid
        async with get_session(self._db_url) as session:
            session.add(EcosystemRepoStatusSnapshotModel.from_pydantic(snapshot))
        return snapshot

    async def list_status_snapshots(
        self,
        repo_id: str = "",
        scan_run_id: str = "",
        limit: int = 100,
        project_id: str | None = None,
    ) -> list[EcosystemRepoStatusSnapshot]:
        """List status snapshots, in snapshot_at descending order.

        Parameters:
            repo_id: filter by repo; empty = no filtering.
            scan_run_id: filter by scan batch; empty = no filtering.
            project_id: explicit scope; when empty, _project_scope injects it automatically.
        """
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoStatusSnapshotModel)
            stmt = self._apply_project_filter(stmt, EcosystemRepoStatusSnapshotModel)
            if project_id:
                stmt = stmt.where(EcosystemRepoStatusSnapshotModel.project_id == project_id)
            if repo_id:
                stmt = stmt.where(EcosystemRepoStatusSnapshotModel.repo_id == repo_id)
            if scan_run_id:
                stmt = stmt.where(
                    EcosystemRepoStatusSnapshotModel.scan_run_id == scan_run_id
                )
            stmt = stmt.order_by(
                EcosystemRepoStatusSnapshotModel.snapshot_at.desc()
            ).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # v1.5.0-A: Project ecosystem settings helpers
    # ================================================================

    async def get_ecosystem_project_settings(
        self,
        project_id: str,
    ) -> EcosystemProjectSettings | None:
        """Fetch a project's ecosystem config by project_id."""
        if not project_id:
            return None
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemProjectSettingsModel).where(
                EcosystemProjectSettingsModel.project_id == project_id
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def upsert_ecosystem_project_settings(
        self,
        settings: EcosystemProjectSettings,
    ) -> EcosystemProjectSettings:
        """Upsert a project's ecosystem config by the project_id primary key.

        If it exists, update all fields (except created_at); otherwise insert.
        updated_at is always set to the current UTC time.
        """
        if not settings.project_id:
            raise ValueError("project_id must not be empty")
        now = utc_now()
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemProjectSettingsModel).where(
                EcosystemProjectSettingsModel.project_id == settings.project_id
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                settings.updated_at = now
                session.add(EcosystemProjectSettingsModel.from_pydantic(settings))
            else:
                row.min_stars = settings.min_stars
                row.top_n = settings.top_n
                row.refresh_interval_days = settings.refresh_interval_days
                row.auto_shallow_on_archive = settings.auto_shallow_on_archive
                row.focus_topics = settings.focus_topics
                row.focus_languages = settings.focus_languages
                row.shallow_concurrency = settings.shallow_concurrency
                row.deep_concurrency = settings.deep_concurrency
                row.updated_at = now
                settings.updated_at = now
        return settings

    async def ensure_ecosystem_project_settings(
        self,
        project_id: str,
        *,
        is_ai_team_os: bool = False,
    ) -> EcosystemProjectSettings:
        """Ensure a project has ecosystem settings; create them with defaults when absent.

        The AI Team OS project uses stricter defaults (min_stars=5000, focus claude/mcp/agent topics);
        other projects use generic defaults (min_stars=1000, top_n=100, focus_topics=[]).

        Idempotent: if it exists, return it directly without overwriting manual edits.
        """
        existing = await self.get_ecosystem_project_settings(project_id)
        if existing is not None:
            return existing
        if is_ai_team_os:
            payload = EcosystemProjectSettings(
                project_id=project_id,
                min_stars=5000,
                top_n=200,
                focus_topics=["claude-code", "mcp", "agent-framework"],
            )
        else:
            payload = EcosystemProjectSettings(
                project_id=project_id,
                min_stars=1000,
                top_n=100,
            )
        return await self.upsert_ecosystem_project_settings(payload)

    # ================================================================
    # Ecosystem shallow batch management (v1.7.0)
    # ================================================================

    async def create_shallow_batch(
        self,
        batch: EcosystemShallowBatch,
    ) -> EcosystemShallowBatch:
        """Create a new shallow-scan batch."""
        async with get_session(self._db_url) as session:
            session.add(EcosystemShallowBatchModel.from_pydantic(batch))
        return batch

    async def get_shallow_batch(
        self,
        batch_id: str,
        project_id: str | None = None,
    ) -> EcosystemShallowBatch | None:
        """Query a shallow-scan batch by ID."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemShallowBatchModel).where(
                EcosystemShallowBatchModel.id == batch_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemShallowBatchModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_shallow_batches(
        self,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EcosystemShallowBatch], int]:
        """List shallow-scan batches (created_at descending). Returns (batches, total)."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            base = select(EcosystemShallowBatchModel)
            count_q = select(func.count()).select_from(EcosystemShallowBatchModel)
            if effective_pid is not None:
                base = base.where(EcosystemShallowBatchModel.project_id == effective_pid)
                count_q = count_q.where(EcosystemShallowBatchModel.project_id == effective_pid)
            total_result = await session.execute(count_q)
            total = total_result.scalar_one() or 0
            stmt = base.order_by(EcosystemShallowBatchModel.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows], total

    async def update_shallow_batch(
        self,
        batch_id: str,
        project_id: str | None = None,
        **kwargs: Any,
    ) -> EcosystemShallowBatch | None:
        """Update a shallow-scan batch's fields (field names/values passed via kwargs)."""
        effective_pid = self._effective_project_id(project_id)
        now = utc_now()
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemShallowBatchModel).where(
                EcosystemShallowBatchModel.id == batch_id
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemShallowBatchModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, val in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, val)
            row.updated_at = now
            await session.flush()
            return row.to_pydantic()

    async def delete_unstarted_batch_reviews(
        self,
        batch_id: str,
        project_id: str | None = None,
    ) -> int:
        """Delete the not-yet-started shallow-scan task rows in a batch (stage_status='queued' and unclaimed).

        Claimed/completed rows are untouched. The inverse operation of approve, used to roll back a batch that will no
        longer run.
        """
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemDeepReviewModel).where(
                EcosystemDeepReviewModel.batch_id == batch_id,
                EcosystemDeepReviewModel.stage_status == "queued",
                EcosystemDeepReviewModel.claimed_by.is_(None),
            )
            if effective_pid is not None:
                stmt = stmt.where(EcosystemDeepReviewModel.project_id == effective_pid)
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                await session.delete(row)
            return len(rows)

    async def get_batch_items(
        self,
        batch_id: str,
        project_id: str | None = None,
    ) -> list[dict]:
        """Get the candidate repo list within a batch (including basic repo info).

        Return format: [{repo_id, repo_full_name, stars, stage_status, shallow_summary_excerpt, last_commit_at}, ...]
        """
        import json

        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            # 1. Pull the batch row to get candidates_snapshot_json
            batch_stmt = select(EcosystemShallowBatchModel).where(
                EcosystemShallowBatchModel.id == batch_id
            )
            if effective_pid is not None:
                batch_stmt = batch_stmt.where(
                    EcosystemShallowBatchModel.project_id == effective_pid
                )
            batch_result = await session.execute(batch_stmt)
            batch_row = batch_result.scalar_one_or_none()
            if batch_row is None:
                return []

            # 2. Parse the candidate snapshot
            repo_ids: list[str] = []
            if batch_row.candidates_snapshot_json:
                try:
                    repo_ids = json.loads(batch_row.candidates_snapshot_json)
                except Exception:
                    repo_ids = []

            if not repo_ids:
                return []

            # 3. Batch-pull the repo profiles' basic fields
            profile_stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.id.in_(repo_ids)
            )
            if effective_pid is not None:
                profile_stmt = profile_stmt.where(
                    EcosystemRepoProfileModel.project_id == effective_pid
                )
            profile_result = await session.execute(profile_stmt)
            profiles = {r.id: r for r in profile_result.scalars().all()}

            # 4. Pull the associated DRs' stage_status (the latest one)
            dr_stmt = (
                select(EcosystemDeepReviewModel.repo_id, EcosystemDeepReviewModel.stage_status)
                .where(EcosystemDeepReviewModel.repo_id.in_(repo_ids))
                .where(EcosystemDeepReviewModel.batch_id == batch_id)
            )
            dr_result = await session.execute(dr_stmt)
            dr_map = {row[0]: row[1] for row in dr_result.all()}

            items = []
            for repo_id in repo_ids:
                profile = profiles.get(repo_id)
                if profile is None:
                    items.append({"repo_id": repo_id, "repo_full_name": repo_id, "stars": 0,
                                  "stage_status": "unknown", "shallow_summary_excerpt": "", "last_commit_at": None})
                    continue
                summary = profile.shallow_summary or ""
                items.append({
                    "repo_id": profile.id,
                    "repo_full_name": profile.repo_full_name,
                    "stars": profile.stars or 0,
                    "stage_status": dr_map.get(repo_id, "queued"),
                    "shallow_summary_excerpt": summary[:200] if summary else "",
                    "last_commit_at": profile.last_commit_at.isoformat() if profile.last_commit_at else None,
                })
            return items


    # ================================================================
    # v1.6.0 P0: DataSource CRUD
    # ================================================================

    async def create_data_source(
        self,
        project_id: str,
        kind: DataSourceKind,
        name: str,
        config: dict | None = None,
    ) -> DataSource:
        """Create a new data source configuration for a project."""
        ds = DataSource(
            project_id=project_id,
            kind=kind,
            name=name,
            config=config or {},
        )
        orm = EcosystemDataSourceModel.from_pydantic(ds)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return ds

    async def list_data_sources(self, project_id: str) -> list[DataSource]:
        """List all data sources for a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemDataSourceModel).where(
                    EcosystemDataSourceModel.project_id == project_id
                )
            )
            rows = result.scalars().all()
        return [r.to_pydantic() for r in rows]

    async def update_data_source(
        self,
        ds_id: str,
        *,
        name: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
    ) -> DataSource:
        """Update a data source and increment its version."""

        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemDataSourceModel).where(
                    EcosystemDataSourceModel.id == ds_id
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"DataSource {ds_id} not found")
            if name is not None:
                row.name = name
            if config is not None:
                row.config_json = config
            if enabled is not None:
                row.enabled = enabled
            row.version = (row.version or 1) + 1
            row.updated_at = utc_now()
            return row.to_pydantic()

    async def disable_data_source(self, ds_id: str) -> None:
        """Disable a data source (soft delete via enabled=False)."""
        await self.update_data_source(ds_id, enabled=False)

    # ================================================================
    # v1.6.0 P0: ScanProfile versioned CRUD
    # ================================================================

    async def get_active_scan_profile(self, project_id: str) -> ScanProfile | None:
        """Return the active scan profile for a project, or None if none exists."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemScanProfileModel)
                .where(
                    EcosystemScanProfileModel.project_id == project_id,
                    EcosystemScanProfileModel.is_active == True,  # noqa: E712
                )
                .order_by(EcosystemScanProfileModel.version.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return row.to_pydantic() if row else None

    async def create_or_update_scan_profile(
        self,
        project_id: str,
        profile_dict: dict,
    ) -> ScanProfile:
        """Create a new scan profile version; deactivate the previous active version.

        Each call creates a new row (version increments). Old is_active rows
        for this project are set to False atomically.
        """
        async with get_session(self._db_url) as session:
            # Deactivate all current active profiles for this project
            result = await session.execute(
                select(EcosystemScanProfileModel).where(
                    EcosystemScanProfileModel.project_id == project_id,
                    EcosystemScanProfileModel.is_active == True,  # noqa: E712
                )
            )
            old_rows = result.scalars().all()
            max_version = 0
            for old in old_rows:
                old.is_active = False
                if (old.version or 0) > max_version:
                    max_version = old.version or 0

            new_profile = ScanProfile(
                project_id=project_id,
                version=max_version + 1,
                profile=profile_dict,
                is_active=True,
            )
            orm = EcosystemScanProfileModel.from_pydantic(new_profile)
            session.add(orm)
        return new_profile

    # ================================================================
    # v1.6.0 P0.4: EcosystemIndexDiff CRUD
    # ================================================================

    async def create_index_diff(self, diff: EcosystemIndexDiff) -> EcosystemIndexDiff:
        """Persist an index diff record."""
        async with get_session(self._db_url) as session:
            orm = EcosystemIndexDiffModel.from_pydantic(diff)
            session.add(orm)
        return diff

    async def get_latest_index_diff(self, project_id: str) -> EcosystemIndexDiff | None:
        """Return the most recent index diff for a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemIndexDiffModel)
                .where(EcosystemIndexDiffModel.project_id == project_id)
                .order_by(EcosystemIndexDiffModel.generated_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_index_diffs(
        self,
        project_id: str,
        limit: int = 10,
    ) -> list[EcosystemIndexDiff]:
        """Return recent index diffs for a project, newest first."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemIndexDiffModel)
                .where(EcosystemIndexDiffModel.project_id == project_id)
                .order_by(EcosystemIndexDiffModel.generated_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # v1.6.0 P0.4: EcosystemStatusChange CRUD
    # ================================================================

    async def create_status_change(self, sc: EcosystemStatusChange) -> EcosystemStatusChange:
        """Persist a single repo status change record."""
        async with get_session(self._db_url) as session:
            orm = EcosystemStatusChangeModel.from_pydantic(sc)
            session.add(orm)
        return sc

    async def bulk_create_status_changes(
        self,
        changes: list[EcosystemStatusChange],
    ) -> int:
        """Bulk insert status change records; returns count inserted."""
        if not changes:
            return 0
        async with get_session(self._db_url) as session:
            for sc in changes:
                session.add(EcosystemStatusChangeModel.from_pydantic(sc))
        return len(changes)

    async def update_repo_active_status(
        self,
        repo_id: str,
        new_status: str,
        popularity_percentile: float | None = None,
        activity_score: float | None = None,
        active_rank: int | None = None,
    ) -> None:
        """Update last_active_status and optional NormalizedSignal fields on a repo profile."""
        from sqlalchemy import update as sa_update

        now = utc_now()
        values: dict = {
            "last_active_status": new_status,
            "last_status_change_at": now,
        }
        if popularity_percentile is not None:
            values["popularity_percentile"] = popularity_percentile
        if activity_score is not None:
            values["activity_score"] = activity_score
        if active_rank is not None:
            values["active_rank"] = active_rank

        async with get_session(self._db_url) as session:
            await session.execute(
                sa_update(EcosystemRepoProfileModel)
                .where(EcosystemRepoProfileModel.id == repo_id)
                .values(**values)
            )

    async def update_repo_manual_status(
        self,
        repo_id: str,
        manual_status: str | None,
        reason: str = "",
        set_by: str = "user",
    ) -> bool:
        """Set or clear manual_status on a repo profile. Returns True if row found."""
        from sqlalchemy import update as sa_update

        now = utc_now()
        values: dict = {
            "manual_status": manual_status,
            "manual_status_reason": reason if manual_status else None,
            "manual_status_set_at": now if manual_status else None,
            "manual_status_set_by": set_by if manual_status else None,
        }
        async with get_session(self._db_url) as session:
            result = await session.execute(
                sa_update(EcosystemRepoProfileModel)
                .where(EcosystemRepoProfileModel.id == repo_id)
                .values(**values)
            )
            return result.rowcount > 0

    async def list_pinned_repos(
        self,
        project_id: str | None = None,
        limit: int = 500,
    ) -> tuple[list[EcosystemRepoProfile], int]:
        """Return all repos with manual_status='pinned' for the given project."""
        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.manual_status == "pinned"
            )
            if effective_pid is None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id.is_(None))
            else:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id == effective_pid)
            stmt = stmt.order_by(EcosystemRepoProfileModel.stars.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            profiles = [r.to_pydantic() for r in rows]
            return profiles, len(profiles)

    async def update_repo_discovered_queries(
        self,
        repo_id: str,
        new_query: str,
    ) -> bool:
        """Union-append new_query into discovered_via_queries JSON array.

        Idempotent: if new_query already present, no change is made.
        Returns True if the row was found.
        """
        import json as _json

        from sqlalchemy import update as sa_update

        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(EcosystemRepoProfileModel).where(
                    EcosystemRepoProfileModel.id == repo_id
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            existing: list[str] = []
            if row.discovered_via_queries:
                try:
                    existing = _json.loads(row.discovered_via_queries)
                except Exception:
                    existing = []
            if new_query not in existing:
                existing.append(new_query)
                await session.execute(
                    sa_update(EcosystemRepoProfileModel)
                    .where(EcosystemRepoProfileModel.id == repo_id)
                    .values(discovered_via_queries=_json.dumps(existing))
                )
            return True

    async def update_repo_discovered_queries_by_name(
        self,
        repo_full_name: str,
        new_query: str,
        project_id: str | None = None,
    ) -> bool:
        """Union-append new_query into discovered_via_queries for a repo looked up by name.

        Returns True if the row was found.
        """
        import json as _json

        from sqlalchemy import update as sa_update

        effective_pid = self._effective_project_id(project_id)
        async with get_session(self._db_url) as session:
            stmt = select(EcosystemRepoProfileModel).where(
                EcosystemRepoProfileModel.repo_full_name == repo_full_name
            )
            if effective_pid is None:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id.is_(None))
            else:
                stmt = stmt.where(EcosystemRepoProfileModel.project_id == effective_pid)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            existing: list[str] = []
            if row.discovered_via_queries:
                try:
                    existing = _json.loads(row.discovered_via_queries)
                except Exception:
                    existing = []
            if new_query not in existing:
                existing.append(new_query)
                await session.execute(
                    sa_update(EcosystemRepoProfileModel)
                    .where(EcosystemRepoProfileModel.id == row.id)
                    .values(discovered_via_queries=_json.dumps(existing))
                )
            return True

    # ================================================================
    # v1.6.0 event sourcing: EcosystemRepoEvent CRUD
    # ================================================================

    async def create_repo_event(self, event: EcosystemRepoEvent) -> EcosystemRepoEvent:
        """Persist a single repo event record."""
        async with get_session(self._db_url) as session:
            orm = EcosystemRepoEventModel.from_pydantic(event)
            session.add(orm)
        return event

    async def bulk_create_repo_events(self, events: list[EcosystemRepoEvent]) -> int:
        """Bulk insert repo event records; returns count inserted."""
        if not events:
            return 0
        async with get_session(self._db_url) as session:
            for ev in events:
                session.add(EcosystemRepoEventModel.from_pydantic(ev))
        return len(events)

    async def list_repo_events(
        self,
        repo_id: str,
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[EcosystemRepoEvent]:
        """Return events for a repo, newest first."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(EcosystemRepoEventModel)
                .where(EcosystemRepoEventModel.repo_id == repo_id)
                .order_by(EcosystemRepoEventModel.triggered_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def query_events_in_period(
        self,
        project_id: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[EcosystemRepoEvent]:
        """Return all events within a time window for a project."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(EcosystemRepoEventModel)
                .where(EcosystemRepoEventModel.project_id == project_id)
                .where(EcosystemRepoEventModel.triggered_at >= from_dt)
                .where(EcosystemRepoEventModel.triggered_at <= to_dt)
                .order_by(EcosystemRepoEventModel.triggered_at.asc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # I3a: Workflow observability (workflow_runs / workflow_agents)
    #
    # The two tables are "rebuildable caches of the immutable file wf_<id>.json": UPSERT by natural key,
    # advancing monotonically, never DELETE-ing rows, never destroying stage_history/audit events (red line 3).
    # Upserts locate by the unique natural key, so they do *not* apply _apply_project_filter (under a scoped repo,
    # a filter miss would cause a duplicate INSERT on the unique wf_id → triggering a unique-constraint error);
    # only the get/list read side applies _apply_project_filter for project isolation.
    # ================================================================

    async def upsert_workflow_run(self, run: WorkflowRun) -> WorkflowRunUpsert:
        """Insert-or-update a workflow run by natural key ``wf_id``.

        Monotonic merge semantics (idempotent, supporting interleaved hook/file arrival):
        - status only rises, never falls (per _WF_STATUS_RANK);
        - source merging (hook + file → 'hook+file');
        - text/integer fields are overwritten only when the new value is non-empty/non-zero, preventing a hook skeleton
        from erasing file telemetry,
          or file telemetry from erasing the hook's planned_agent_count.

        Returns :class:`WorkflowRunUpsert` (run + became_completed + became_terminal):
        the judgment of "whether this is the first transition to completed / first entry into a terminal state" is
        folded into this transaction (read old status →
        write new status, same session), for the caller to emit a one-time event. Reading was_completed outside the
        transaction and
        then emitting would double-emit when the three unserialized ingest drivers interleave (audit WP10).
        """
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WorkflowRunModel).where(WorkflowRunModel.wf_id == run.wf_id)
            )
            row = result.scalar_one_or_none()
            now = utc_now()
            if row is None:
                orm = WorkflowRunModel.from_pydantic(run)
                orm.updated_at = now
                session.add(orm)
                await session.flush()
                # First seen already in a terminal state: this is that terminal state's first persistence → report the
                # transition.
                became_completed = orm.status == "completed"
                became_terminal = orm.status in _WF_TERMINAL_STATUSES
                return WorkflowRunUpsert(
                    orm.to_pydantic(), became_completed, became_terminal
                )

            # Record the old state (before writing), to judge terminal-state transitions within this transaction.
            old_status = row.status
            old_terminal = old_status in _WF_TERMINAL_STATUSES

            # status: only rises, never falls
            if run.status:
                old_rank = _WF_STATUS_RANK.get(row.status, 0)
                new_rank = _WF_STATUS_RANK.get(run.status, 0)
                if new_rank >= old_rank:
                    row.status = run.status

            # source: merge with dedup
            row.source = _merge_wf_source(row.source, run.source)

            # Text/association fields: a new non-empty value wins
            for fld in ("name", "summary", "script_path", "transcript_dir",
                        "cc_task_id", "session_id", "team_id", "project_id"):
                val = getattr(run, fld)
                if val:
                    setattr(row, fld, val)

            # JSON fields: a new non-empty/non-None value wins
            if run.phases:
                row.phases = run.phases
            if run.result is not None:
                row.result = run.result

            # Integer counts: a new non-zero value wins (protecting hook planned_* and file agent_count from overwriting
            # each other)
            for fld in ("planned_agent_count", "dynamic_nodes", "agent_count",
                        "total_tokens", "total_tool_calls"):
                val = getattr(run, fld)
                if val:
                    setattr(row, fld, val)

            # Times: a new non-None value wins
            if run.duration_ms is not None:
                row.duration_ms = run.duration_ms
            if run.started_at is not None:
                row.started_at = run.started_at
            if run.completed_at is not None:
                row.completed_at = run.completed_at

            # Phase2 watermark/live columns: overwritten only when the caller explicitly carries them (non-None),
            # allowing an explicit 0/'' reset
            # — never apply the "new non-zero wins" integer rule above (otherwise journal_offset could never return to
            # 0, and
            # source_fingerprint could not be cleared to fall back to mtime).
            for fld in ("journal_offset", "source_fingerprint", "live_tokens"):
                val = getattr(run, fld)
                if val is not None:
                    setattr(row, fld, val)
            # last_activity_at monotonically takes max (file mtimes are naturally monotonic; guards against out-of-order
            # ticks/clock rollbacks)
            if run.last_activity_at is not None and (
                row.last_activity_at is None or run.last_activity_at > row.last_activity_at
            ):
                row.last_activity_at = run.last_activity_at

            row.updated_at = now
            await session.flush()
            # Terminal-state transition judgment (post-write row.status vs the transaction-start old_status):
            # completed's rank 3 is always >= any old rank, so when status==completed it has necessarily been written.
            # Same-rank conversions among completed↔killed↔failed also count as "first entry into that terminal state" —
            # consistent with the old
            # was_completed (prev.status=='completed') semantics (an old killed→completed would emit).
            became_completed = row.status == "completed" and old_status != "completed"
            became_terminal = (
                row.status in _WF_TERMINAL_STATUSES and not old_terminal
            )
            return WorkflowRunUpsert(
                row.to_pydantic(), became_completed, became_terminal
            )

    async def upsert_workflow_agent(self, agent: WorkflowAgent) -> WorkflowAgent:
        """Insert-or-update a workflow fan-out agent by ``(wf_id, cc_agent_id)``.

        The file is the telemetry source of truth, so a new non-empty/non-None value overrides the existing one;
        None/empty keeps the original value
        (e.g. when os_agent_id is not yet associated, do not erase a previous association).
        """
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WorkflowAgentModel).where(
                    WorkflowAgentModel.wf_id == agent.wf_id,
                    WorkflowAgentModel.cc_agent_id == agent.cc_agent_id,
                )
            )
            row = result.scalar_one_or_none()
            now = utc_now()
            if row is None:
                orm = WorkflowAgentModel.from_pydantic(agent)
                orm.updated_at = now
                session.add(orm)
                await session.flush()
                return orm.to_pydantic()

            for fld in (
                "run_id", "project_id", "os_agent_id", "label", "phase_index",
                "phase_title", "model", "state", "tokens", "tool_calls",
                "duration_ms", "last_tool_name", "last_tool_summary",
                "prompt_preview", "result_preview", "started_at", "queued_at",
                "last_activity_at",
            ):
                val = getattr(agent, fld)
                if val is not None and val != "":
                    setattr(row, fld, val)
            row.updated_at = now
            await session.flush()
            return row.to_pydantic()

    async def get_workflow_run(self, wf_id: str) -> WorkflowRun | None:
        """Get a workflow run by wf_id (project-scoped when scope set)."""
        async with get_session(self._db_url) as session:
            stmt = select(WorkflowRunModel).where(WorkflowRunModel.wf_id == wf_id)
            stmt = self._apply_project_filter(stmt, WorkflowRunModel)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_workflow_runs_by_team(self, team_id: str) -> list[WorkflowRun]:
        """Every run linked to a team via ``workflow_runs.team_id``.

        Deliberately skips ``_apply_project_filter``: the only caller decides whether
        a workflow team is finished, and a run hidden by project scope would read as
        "no runs left" — i.e. it could close a team whose run is still going. Missing
        a run must never be possible here, so scope narrowing is not applied.
        """
        if not team_id:
            return []
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WorkflowRunModel).where(WorkflowRunModel.team_id == team_id)
            )
            return [r.to_pydantic() for r in result.scalars().all()]

    async def list_workflow_runs(
        self,
        project_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[WorkflowRun]:
        """List workflow runs, newest-first, optionally filtered by project/status."""
        async with get_session(self._db_url) as session:
            stmt = select(WorkflowRunModel)
            if project_id:
                stmt = stmt.where(WorkflowRunModel.project_id == project_id)
            if status:
                stmt = stmt.where(WorkflowRunModel.status == status)
            stmt = self._apply_project_filter(stmt, WorkflowRunModel)
            # Display order = actual run time first (falling back to ingestion time when started_at is missing):
            # historical bulk
            # absorption crowds created_at onto the same day, and sorting by it would bury the latest runs beyond the
            # pagination
            #(the root cause of the user's 2026-07-06 measurement "no records after 6.29").
            stmt = stmt.order_by(
                func.coalesce(
                    WorkflowRunModel.started_at, WorkflowRunModel.created_at
                ).desc()
            ).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def aggregate_model_usage(self, days: int = 7) -> list[dict[str, Any]]:
        """Aggregate the last N days of workflow agent usage by model tier (the orchestration charter's observation
        semantics).

        Data source: workflow_agents (model/tokens backfilled at terminal state), in tokens descending order.
        """
        # The cutoff and workflow_agents.updated_at are both UTC (the whole DB's single clock), so they can be compared
        # directly.
        # Historical note: this was once an accident site of the dual-wall-clock era — the cutoff was UTC while the
        # column was written in local time, so in UTC+8
        # "the last N days" actually counted N days + 8 hours; no exception, just numbers quietly inflated.
        cutoff = utc_now() - timedelta(days=days)
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    WorkflowAgentModel.model,
                    func.count().label("agents"),
                    func.coalesce(func.sum(WorkflowAgentModel.tokens), 0).label(
                        "tokens"
                    ),
                )
                .where(WorkflowAgentModel.updated_at >= cutoff)
                .group_by(WorkflowAgentModel.model)
                .order_by(func.coalesce(func.sum(WorkflowAgentModel.tokens), 0).desc())
            )
            stmt = self._apply_project_filter(stmt, WorkflowAgentModel)
            result = await session.execute(stmt)
            return [
                {"model": model or "(not recorded)", "agents": agents, "tokens": tokens}
                for model, agents, tokens in result.all()
            ]

    async def list_workflow_agents(self, wf_id: str) -> list[WorkflowAgent]:
        """List all fan-out agents for a workflow run (ordered by phase then label)."""
        async with get_session(self._db_url) as session:
            stmt = select(WorkflowAgentModel).where(WorkflowAgentModel.wf_id == wf_id)
            stmt = self._apply_project_filter(stmt, WorkflowAgentModel)
            stmt = stmt.order_by(
                WorkflowAgentModel.phase_index.asc(),
                WorkflowAgentModel.label.asc(),
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_workflow_agents_by_cc_id(
        self, cc_agent_id: str
    ) -> list[WorkflowAgent]:
        """Reverse-look-up an observation row by cc_agent_id (used by the receipt migration's negative exclusion:
        judging which run a member truly belongs to)."""
        async with get_session(self._db_url) as session:
            stmt = select(WorkflowAgentModel).where(
                WorkflowAgentModel.cc_agent_id == cc_agent_id
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Knowledge links (knowledge layer P1a — cross-domain reference graph, append-only)
    # ================================================================

    async def insert_knowledge_links(self, links: list[KnowledgeLink]) -> int:
        """Batch-insert reference edges, silently ignoring UNIQUE conflicts (idempotent; can be re-scanned
        repeatedly)."""
        if not links:
            return 0
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        n = 0
        async with get_session(self._db_url) as session:
            for lk in links:
                stmt = (
                    sqlite_insert(KnowledgeLinkModel)
                    .values(
                        from_kind=lk.from_kind,
                        from_id=lk.from_id,
                        to_kind=lk.to_kind,
                        to_id=lk.to_id,
                        link_type=lk.link_type,
                        context=lk.context[:500],
                        link_source=lk.link_source,
                        project_id=lk.project_id or "",
                        created_at=lk.created_at,
                    )
                    .on_conflict_do_nothing(index_elements=[
                        "from_kind", "from_id", "to_kind", "to_id", "link_type",
                    ])
                )
                result = await session.execute(stmt)
                n += int(result.rowcount or 0)
        return n

    async def find_knowledge_links(
        self,
        kind: str,
        id_: str,
        direction: str = "both",
        limit: int = 100,
    ) -> list[KnowledgeLink]:
        """Query edges by endpoint. direction: out (whom this object references) / in (who references it) / both."""
        async with get_session(self._db_url) as session:
            conds = []
            if direction in ("out", "both"):
                conds.append(
                    (KnowledgeLinkModel.from_kind == kind)
                    & (KnowledgeLinkModel.from_id == id_)
                )
            if direction in ("in", "both"):
                conds.append(
                    (KnowledgeLinkModel.to_kind == kind)
                    & (KnowledgeLinkModel.to_id == id_)
                )
            from sqlalchemy import or_

            stmt = (
                select(KnowledgeLinkModel)
                .where(or_(*conds))
                .order_by(KnowledgeLinkModel.created_at.desc())
                .limit(max(1, min(limit, 500)))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [r.to_pydantic() for r in rows]

    async def knowledge_link_fanout(
        self,
        seed_kind: str,
        seed_id: str,
        depth: int = 2,
        limit: int = 50,
    ) -> list[dict]:
        """Undirected recursive fan-out (depth clamped to 1-2, following GBrain's relationalFanout idea).

        Returns reachable nodes [{kind, id, hop, via_types, path}], in hop ascending order.
        """
        from sqlalchemy import text as sa_text

        depth = max(1, min(int(depth), 2))
        limit = max(1, min(int(limit), 200))
        sql = sa_text(
            """
            WITH RECURSIVE walk(kind, id, hop, via, path) AS (
                SELECT :seed_kind, :seed_id, 0, '', :seed_kind || ':' || :seed_id
                UNION
                SELECT
                    CASE WHEN l.from_kind = w.kind AND l.from_id = w.id
                         THEN l.to_kind ELSE l.from_kind END,
                    CASE WHEN l.from_kind = w.kind AND l.from_id = w.id
                         THEN l.to_id ELSE l.from_id END,
                    w.hop + 1,
                    l.link_type,
                    w.path || ' -> ' ||
                    CASE WHEN l.from_kind = w.kind AND l.from_id = w.id
                         THEN l.to_kind || ':' || l.to_id
                         ELSE l.from_kind || ':' || l.from_id END
                FROM knowledge_links l
                JOIN walk w ON (
                    (l.from_kind = w.kind AND l.from_id = w.id)
                    OR (l.to_kind = w.kind AND l.to_id = w.id)
                )
                WHERE w.hop < :depth
                  AND instr(w.path, CASE WHEN l.from_kind = w.kind AND l.from_id = w.id
                        THEN l.to_kind || ':' || l.to_id
                        ELSE l.from_kind || ':' || l.from_id END) = 0
            )
            SELECT kind, id, MIN(hop) AS hop,
                   GROUP_CONCAT(DISTINCT via) AS via_types,
                   MIN(path) AS path
            FROM walk WHERE hop > 0
            GROUP BY kind, id
            ORDER BY hop ASC
            LIMIT :lim
            """
        )
        async with get_session(self._db_url) as session:
            rows = (
                await session.execute(
                    sql,
                    {"seed_kind": seed_kind, "seed_id": seed_id,
                     "depth": depth, "lim": limit},
                )
            ).all()
            return [
                {"kind": r[0], "id": r[1], "hop": r[2],
                 "via_types": (r[3] or "").split(","), "path": r[4]}
                for r in rows
            ]

    # ================================================================
    # Governance leader lease (D3 stage C, audit M50)
    # ================================================================

    async def release_governance_lease(self, holder: str) -> bool:
        """Actively release the governance lease (only when self is the holder). Returns whether it was really released.

        Not releasing also self-heals — the lease has a TTL — but that means waiting out the full TTL
        (3×REAPER_CHECK_INTERVAL=180s),
        during which governance is silent across the board (no collection/advancement/scheduling/waking/reconciliation
        runs). API restarts are frequent in development;
        measured 2026-07-26: after a restart, the new instance was locked out for nearly 3 minutes by the lease of the
        old pid it had just killed,
        making fixes like contradiction-state self-healing look "ineffective". Yield the lease on graceful shutdown so
        the new instance takes over immediately.
        """
        from sqlalchemy import text

        async with get_session(self._db_url) as session:
            result = await session.execute(
                text(
                    "UPDATE governance_lease SET holder = '', expires_at = NULL, "
                    "updated_at = :now WHERE id = 'governance' AND holder = :holder"
                ),
                {"holder": holder, "now": utc_now().isoformat()},
            )
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def try_acquire_governance_lease(self, holder: str, ttl_seconds: int) -> bool:
        """Atomically acquire/renew the governance leader lease (single row id='governance').

        When multiple API instances coexist, only the holder runs background governance. Reuses the v1.5.3 claim
        paradigm —
        SQLite serialized writes + rowcount are the sole source of truth for "did I really win it"; never use file locks
        (a history of four rollovers). Winning conditions: same-holder renewal, or the row is ownerless/the lease has
        expired.
        Timestamps are UTC ISO strings, compared lexicographically only (equivalent to time order under the same
        format).

        A2-obs (debate 503e07f1, topic A): after winning, if the displaced holder is a predecessor **still hanging on
        the row
        but whose lease has expired**, additionally record a GOVERNANCE_LEASE_TAKEN_OVER event. No new column,
        no schema change, pure observation — first answer "has governance ever changed hands"; if there is not a single
        one in the observation period,
        A2-impl (the epoch column) need not be done.
        """
        from sqlalchemy import text

        now = utc_now()
        now_str = now.isoformat()
        expires_str = (now + timedelta(seconds=ttl_seconds)).isoformat()

        async with get_session(self._db_url) as session:
            await session.execute(
                text(
                    "INSERT OR IGNORE INTO governance_lease "
                    "(id, holder, expires_at, updated_at) "
                    "VALUES ('governance', '', NULL, :now)"
                ),
                {"now": now_str},
            )
            # One read for pure observation: the judgment right still lies solely in the UPDATE's WHERE below; this read
            # does not participate in win/loss,
            # it only answers "from whom did I take over". In theory another instance can still slip in between the
            # SELECT and the UPDATE,
            # so the predecessor read is best-effort — the observation surface can tolerate this much error,
            # but the judgment surface cannot, so the source of truth has not moved a single step.
            prior = (
                await session.execute(
                    text("SELECT holder, expires_at FROM governance_lease WHERE id = 'governance'")
                )
            ).first()
            result = await session.execute(
                text(
                    "UPDATE governance_lease "
                    "SET holder = :holder, expires_at = :expires, updated_at = :now "
                    "WHERE id = 'governance' "
                    "AND (holder = :holder OR holder = '' "
                    "OR expires_at IS NULL OR expires_at < :now)"
                ),
                {"holder": holder, "expires": expires_str, "now": now_str},
            )
            await session.commit()
            acquired = bool(result.rowcount and result.rowcount > 0)

        if acquired and prior is not None:
            prev_holder = prior[0] or ""
            prev_expires = prior[1]
            # Recognizes only "the predecessor is in place and the lease is due". Renewal (same holder), ownerless
            # claiming (empty holder),
            # and takeover after an active release are not handovers. A non-empty holder with expires_at IS NULL is
            # a deformed shape only a hand-written row can produce (a release always clears both fields together); per
            # the ruling semantics it does not count.
            if (
                prev_holder
                and prev_holder != holder
                and prev_expires
                and str(prev_expires) < now_str
            ):
                # An event-write failure must never drag down the lease itself — governance is the main flow;
                # observation is a side channel.
                # But never silently either: swallowing the exception would disguise "observation is not running" as "no
                # handover ever happened".
                try:
                    await self.create_event(
                        event_type=EventType.GOVERNANCE_LEASE_TAKEN_OVER.value,
                        source="repository",
                        data={
                            "previous_holder": prev_holder,
                            "new_holder": holder,
                            "previous_expires_at": str(prev_expires),
                            "taken_over_at": now_str,
                        },
                        entity_id="governance",
                        entity_type="governance_lease",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to write the governance lease takeover event (the lease was acquired normally; "
                        "governance is unaffected): "
                        "%s → %s, err=%s",
                        prev_holder,
                        holder,
                        exc,
                    )

        return acquired

    async def count_project_sessions(self, project_id: str) -> int:
        """The number of CC sessions this project has launched — with the file system as the source of truth.

        Counts the ``~/.claude/projects/<slug>/*.jsonl`` session transcript files (the slug computation is the same
        formula as
        workflow_ingest._project_slug: every non-alphanumeric character → '-').
        Deliberately not using the agents.session_id ownership footprint: ownership drifts with heal/absorption
        strategies and
        once made the count constantly 0 (measured 2026-07-07); transcript files do not lie.
        """
        import re as _re
        from pathlib import Path as _Path

        try:
            project = await self.get_project(project_id)
        except Exception:  # noqa: BLE001
            project = None
        root = getattr(project, "root_path", "") if project else ""
        if not root or root.startswith("auto-"):
            return 0
        slug = _re.sub(r"[^a-zA-Z0-9]", "-", root)
        try:
            d = _Path.home() / ".claude" / "projects" / slug
            if not d.is_dir():
                return 0
            return sum(1 for f in d.glob("*.jsonl") if f.is_file())
        except Exception:  # noqa: BLE001
            return 0

    # ================================================================
    # Name resolution for agent → task edges (§2.4)
    # ================================================================

    async def find_teams_by_owner_session(self, session_id: str) -> list[str]:
        """The team id owned by this CC session (the criterion is from the same origin as
        hook_translator._team_owned_by_session).

        The two signals in the same order: the ``config.owner_session_id`` stamped at team creation takes priority; old
        rows fall back to
        "the container team name encodes the first 8 characters of the session id". The name fallback takes effect
        **only when there is no stamp** — otherwise a team
        clearly owned by someone else would be mistaken for one's own because its name collides on 8 hex characters.
        """
        if not session_id:
            return []
        owner = func.json_extract(TeamModel.config, "$.owner_session_id")
        async with get_session(self._db_url) as session:
            stmt = select(TeamModel.id).where(
                or_(
                    owner == session_id,
                    and_(
                        or_(owner.is_(None), owner == ""),
                        TeamModel.name == f"session-{session_id[:8]}",
                    ),
                )
            )
            return [row[0] for row in (await session.execute(stmt)).all()]

    async def find_latest_agent_by_name(
        self,
        name: str,
        *,
        session_id: str = "",
        team_ids: list[str] | None = None,
    ) -> Agent | None:
        """Among same-named agent rows, the one with the latest ``created_at`` **within the limited domain**.

        The domain (``session_id`` or ``team_ids``) **must provide at least one**; with both empty, return None directly
        rather than searching the whole table. This is not defensive programming; it is this method's entire reason to
        exist: ``name`` in the agents
        table is fundamentally not unique — measured: the single name ``Leader`` corresponds to 117 rows spanning 79
        teams. Taking the latest by name across the whole table
        is equivalent to mistaking another session's Leader from three weeks ago for the current one; the edge written
        from it can still be queried and
        raises no error — it just binds the wrong object.

        Case-insensitive: a memo's ``author`` default is ``"leader"``, while a Leader row's
        ``name`` is ``"Leader"``. Strict comparison would prevent the largest share of bookkeeping actions from building
        a single edge.
        """
        cleaned = (name or "").strip()
        if not cleaned or (not session_id and not team_ids):
            return None
        domain = []
        if session_id:
            domain.append(AgentModel.session_id == session_id)
        if team_ids:
            domain.append(AgentModel.team_id.in_(team_ids))
        stmt = (
            select(AgentModel)
            .where(func.lower(AgentModel.name) == cleaned.lower())
            .where(or_(*domain))
            .order_by(AgentModel.created_at.desc())
            .limit(1)
        )
        async with get_session(self._db_url) as session:
            row = (await session.execute(stmt)).scalars().first()
            return row.to_pydantic() if row is not None else None

    # ================================================================
    # Token usage attribution (docs/token-attribution-v1-design.md stage 2)
    #
    # Read-only aggregation. API endpoints and MCP tools both go through these two methods rather than each writing its
    # own SQL —
    # once the denominator has a second implementation, the two places will quietly give different coverage after some
    # change.
    # ================================================================

    def _dispatch_population_filter(self, stmt: Any, population: DispatchPopulation) -> Any:
        """Turn the dispatch path into a WHERE condition. The denominator's definition lives on this line, and only this
        line.

        The denominator counts ``agents`` **rows**, and the equation "one row = one dispatch" is maintained by the
        registration side:
        SubagentStart's reuse criterion recognizes only the dispatch identity (``services.agent_identity``); the same
        name does not
        constitute reuse. Before the A-06 fix, that criterion was by name, so multiple same-named dispatches folded into
        one row, making historical
        windows' denominators too small and coverage too optimistic — see the caliber note for SUBAGENT rows in
        :meth:`usage_coverage_report`.

        """
        if population is DispatchPopulation.SUBAGENT:
            return stmt.where(AgentModel.role != LEADER_ROLE)
        if population is DispatchPopulation.LEADER_SESSION:
            return stmt.where(AgentModel.role == LEADER_ROLE)
        msg = (
            f"population={population.value} is not a population on the agents table — "
            f"workflow_self_report uses workflow_agents self-reported values, and tool_call is not collected "
            f"by design; "
            f"both have coverage only and no four-layer usage — see usage_coverage_report()"
        )
        raise ValueError(msg)

    def _attribution_scope_filter(
        self, stmt: Any, scope: AttributionScope, scope_id: str
    ) -> Any:
        """Turn a scope into a WHERE condition; an empty ``scope_id`` means no filtering on that dimension (whole
        DB)."""
        if not scope_id:
            return stmt
        if scope is AttributionScope.PROJECT:
            return stmt.where(AgentModel.project_id == scope_id)
        if scope is AttributionScope.SESSION:
            return stmt.where(AgentModel.session_id == scope_id)
        if scope is AttributionScope.AGENT:
            return stmt.where(AgentModel.id == scope_id)
        if scope is AttributionScope.WORKFLOW_RUN:
            return stmt.where(
                AgentModel.id.in_(
                    select(WorkflowAgentModel.os_agent_id).where(
                        WorkflowAgentModel.run_id == scope_id
                    )
                )
            )
        # TASK: only agents that left bookkeeping entries enter this scope (edges are produced by §2.4's parasitic
        # writes)
        return stmt.where(AgentModel.id.in_(_worked_on_agents_subquery(scope_id)))

    async def aggregate_token_attribution(
        self,
        *,
        metric: TokenMetric,
        scope: AttributionScope = AttributionScope.PROJECT,
        scope_id: str = "",
        population: DispatchPopulation = DispatchPopulation.SUBAGENT,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> TokenAttribution:
        """Aggregate the ``usage_sum`` four-layer usage by scope, returning it together with the numerator/denominator
        and the unattributed classification.

        ``metric`` is required and currently accepts only ``USAGE_SUM``: ``ctx_last``'s four-layer decomposition was
        never stored
        (``workflow_agents.tokens`` is already the sum of the four fields at ingest time); forcing it into this
        mandatory four-layer
        split structure would require fabricating numbers. Its coverage goes through :meth:`usage_coverage_report`.

        The window falls on ``created_at``, **not on ``tokens_measured_at``** — the latter would erase unmeasured
        rows straight from the denominator, making coverage always 100% (§4.1, the faking method R2 named).
        """
        if metric is not TokenMetric.USAGE_SUM:
            msg = (
                f"metric={metric.value} does not support four layers of attribution: ctx_last is the final-round "
                f"context watermark, "
                f"and its four-layer decomposition was merged into a single number at ingest time and never stored "
                f"(§1.1). "
                f"See usage_coverage_report() for its numerator and denominator, but there is no splittable "
                f"four-layer usage."
            )
            raise ValueError(msg)
        if scope is AttributionScope.TASK and not scope_id:
            # An empty task_id would degrade "unattributed" as a whole into a lie: once the scope filter is skipped,
            # rows without a single task edge
            # also fall into the complement of "not single-edged" and get labeled multi_task_unsplittable
            # — "cannot be split" and "has no edge at all" are two different things; the latter is a task-edge coverage
            # problem (0.8%), and
            # mixing it into the former would hide the narrowest hop of the whole chain.
            msg = (
                "scope=task requires scope_id: task-level attribution is a per-task question. "
                "For global task-edge coverage, see the agent->task hop in usage_coverage_report()."
            )
            raise ValueError(msg)

        measured = AgentModel.tokens_measured_at.is_not(None)
        # The TASK scope has one more gate: an agent that left bookkeeping entries across multiple tasks has four-layer
        # numbers that are a single total
        # over its whole lifecycle, with no per-interval usage to split by edge in time order — §2.4 fixes that it is
        # then honestly counted as unattributed,
        # with averaging allocation forbidden. So it enters neither the numerator nor contributes any tokens.
        single_task = AgentModel.id.in_(_single_worked_on_agents_subquery())
        attributed = and_(measured, single_task) if scope is AttributionScope.TASK else measured

        layer_sums = [
            func.coalesce(func.sum(case((attributed, col), else_=0)), 0)
            for col in (
                AgentModel.input_tokens,
                AgentModel.output_tokens,
                AgentModel.cache_creation_tokens,
                AgentModel.cache_read_tokens,
            )
        ]
        stmt = select(
            func.count().label("total"),
            func.coalesce(func.sum(case((attributed, 1), else_=0)), 0).label("attributed"),
            *layer_sums,
            func.min(case((attributed, AgentModel.tokens_measured_at))).label("win_from"),
            func.max(case((attributed, AgentModel.tokens_measured_at))).label("win_to"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(attributed, AgentModel.tokens_source == TokenSource.ALIAS_FALLBACK.value),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("alias_rows"),
        ).select_from(AgentModel)
        stmt = self._dispatch_population_filter(stmt, population)
        stmt = self._attribution_scope_filter(stmt, scope, scope_id)
        stmt = _apply_created_window(stmt, since, until)

        # The transcript paths of unattributed rows — fetch them back and probe one by one to separate "recoverable"
        # from "unrecoverable".
        # This is the only time this method touches the file system: the classification cannot be derived from the DB,
        # and without classifying,
        # "22% unattributed" would be an unactionable statement (§3.4).
        detail = select(AgentModel.transcript_path, AgentModel.id).select_from(AgentModel)
        detail = self._dispatch_population_filter(detail, population)
        detail = self._attribution_scope_filter(detail, scope, scope_id)
        detail = _apply_created_window(detail, since, until).where(~attributed)

        async with get_session(self._db_url) as session:
            row = (await session.execute(stmt)).one()
            unattributed_rows = (await session.execute(detail)).all()
            multi_task_ids: set[str] = set()
            if scope is AttributionScope.TASK:
                multi = select(AgentModel.id).select_from(AgentModel)
                multi = self._attribution_scope_filter(multi, scope, scope_id)
                multi = multi.where(~AgentModel.id.in_(_single_worked_on_agents_subquery()))
                multi_task_ids = {r[0] for r in (await session.execute(multi)).all()}

        reasons: dict[str, int] = {}
        for transcript_path, agent_id in unattributed_rows:
            if agent_id in multi_task_ids:
                code = UnattributedReason.MULTI_TASK_UNSPLITTABLE.value
            else:
                code = classify_unattributed(transcript_path)
            reasons[code] = reasons.get(code, 0) + 1

        window = (row.win_from, row.win_to) if row.win_from and row.win_to else None
        return TokenAttribution(
            scope=scope,
            scope_id=scope_id,
            metric=TokenMetric.USAGE_SUM,
            input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            cache_creation_tokens=int(row[4] or 0),
            cache_read_tokens=int(row[5] or 0),
            dispatches_attributed=int(row.attributed or 0),
            dispatches_total=int(row.total or 0),
            unattributed_reasons=reasons,
            measured_window=window,
            # As long as one row was derived via an alias fallback, the whole answer is labeled downgraded — caliber
            # honesty over good looks.
            method=(
                AttributionMethod.ALIAS_FALLBACK
                if int(row.alias_rows or 0) > 0
                else AttributionMethod.TRANSCRIPT_PARSE
            ),
        )

    async def usage_coverage_report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageCoverageReport:
        """The coverage vector (§4.1) — the matrix rows + per-hop C_hop, with **zero token values**.

        This is the shared data source of the page's first block on the first screen and ``os_health_check``'s summary
        line. The matrix deliberately
        provides no total row: ``usage_sum`` and ``ctx_last`` differ by a measured 5~25×, so a cross-row total would mix
        calibers.
        """
        sub = await self.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM,
            population=DispatchPopulation.SUBAGENT,
            since=since,
            until=until,
        )
        leader = await self.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM,
            population=DispatchPopulation.LEADER_SESSION,
            since=since,
            until=until,
        )

        non_leader = AgentModel.role != LEADER_ROLE
        hop_specs: list[tuple[str, Any, str]] = [
            ("agent->session", AgentModel.session_id.is_not(None), "Derived and backfilled from the transcript path "
                "(stage 1)"),
            ("agent->project", AgentModel.project_id.is_not(None), "Carried in by team ownership at dispatch time"),
            (
                "agent->workflow",
                AgentModel.id.in_(select(WorkflowAgentModel.os_agent_id)),
                "workflow_agents.os_agent_id match",
            ),
            ("agent->transcript", AgentModel.transcript_path.is_not(None), "The prerequisite for re-collection; "
                "determines the recoverable share"),
            (
                "agent->task",
                AgentModel.id.in_(_worked_on_agents_subquery()),
                "The narrowest hop of the whole chain: task edges are collected parasitically on bookkeeping actions "
                "(§2.4),"
                "so end-to-end task-level attribution can never exceed it (R5 requires same-screen disclosure)",
            ),
        ]

        async with get_session(self._db_url) as session:
            wf_stmt = select(
                func.count().label("total"),
                func.coalesce(
                    func.sum(case((WorkflowAgentModel.tokens > 0, 1), else_=0)), 0
                ).label("reported"),
            ).select_from(WorkflowAgentModel)
            wf_stmt = _apply_created_window(wf_stmt, since, until, WorkflowAgentModel)
            wf = (await session.execute(wf_stmt)).one()

            activities_stmt = select(func.count()).select_from(AgentActivityModel)
            if since is not None:
                activities_stmt = activities_stmt.where(AgentActivityModel.timestamp >= since)
            if until is not None:
                activities_stmt = activities_stmt.where(AgentActivityModel.timestamp < until)
            activities = int((await session.execute(activities_stmt)).scalar() or 0)

            hops: list[EdgeCoverage] = []
            for edge, condition, note in hop_specs:
                stmt = select(
                    func.count().label("required"),
                    func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label("resolvable"),
                ).select_from(AgentModel).where(non_leader)
                stmt = _apply_created_window(stmt, since, until)
                r = (await session.execute(stmt)).one()
                hops.append(
                    EdgeCoverage(
                        edge=edge,
                        resolvable=int(r.resolvable or 0),
                        required=int(r.required or 0),
                        note=note,
                    )
                )

        wf_total = int(wf.total or 0)
        wf_reported = int(wf.reported or 0)
        rows = [
            UsageCoverageRow(
                path=DispatchPopulation.SUBAGENT,
                metric=TokenMetric.USAGE_SUM.value,
                dispatches_total=sub.dispatches_total,
                dispatches_attributed=sub.dispatches_attributed,
                unattributed_reasons=sub.unattributed_reasons,
                note=(
                    "Denominator = the number of registered dispatch rows (one row = one dispatch = one transcript). "
                    "This equation holds since the A-06 fix; before that, SubagentStart reused by name, so multiple "
                    "same-named dispatches folded into one row, making historical windows denominators too small and "
                    "coverage too optimistic, and the folded "
                    "dispatches ledgers were overwritten and are unrecoverable"
                ),
            ),
            UsageCoverageRow(
                path=DispatchPopulation.LEADER_SESSION,
                metric=TokenMetric.USAGE_SUM.value,
                dispatches_total=leader.dispatches_total,
                dispatches_attributed=leader.dispatches_attributed,
                unattributed_reasons=leader.unattributed_reasons,
                note="The main session magnitude far exceeds the sum of all sub-agents; presented in a separate column "
                     "from the row above and not merged by default (§3.3)",
            ),
            UsageCoverageRow(
                path=DispatchPopulation.WORKFLOW_SELF_REPORT,
                metric=TokenMetric.CTX_LAST.value,
                dispatches_total=wf_total,
                dispatches_attributed=wf_reported,
                unattributed_reasons=(
                    {UnattributedReason.SELF_REPORT_ABSENT.value: wf_total - wf_reported}
                    if wf_total > wf_reported
                    else {}
                ),
                note="Different caliber; cannot be added to the two rows above; missing self-reports cannot be "
                     "recovered (the wf JSON never carried telemetry)",
            ),
            UsageCoverageRow(
                path=DispatchPopulation.TOOL_CALL,
                metric="",
                # None rather than 0: on this path the question "how much was measured" does not apply, and 0 would be
                # read as
                # "never measured once". "Not collected by design" is a formal value, not a blank (§3.2 / §5.2).
                dispatches_total=None,
                dispatches_attributed=None,
                note=(
                    f"Not collected by design: {activities} activity records have no token field. Tool calls are "
                    f"per-call "
                    f"granularity while billing is per-request granularity; the two do not correspond one-to-one "
                    f"(§3.2, a deliberate choice, not an omission)"
                ),
            ),
        ]
        return UsageCoverageReport(
            rows=rows,
            hops=hops,
            window=(since, until) if since is not None and until is not None else None,
        )

    async def sample_unattributed(
        self,
        *,
        population: DispatchPopulation = DispatchPopulation.SUBAGENT,
        since: datetime | None = None,
        until: datetime | None = None,
        scan_limit: int = 400,
        per_reason: int = 5,
    ) -> dict[str, Any]:
        """Drill-down data for the unattributed drawer (§5.2 item 2) — a few real sample rows per reason code.

        **Samples are samples, not the denominator**: this scans only the most recent ``scan_limit`` unattributed
        dispatch rows, stopping once each class has
        a few rows. Each class's **count** follows :meth:`usage_coverage_report`'s full scan,
        and the two have different duties — the drawer answers "what does this class look like, and is it recoverable",
        not "how many are there".
        Including ``scanned`` in the return value is exactly so this boundary can be stated on the page.

        ``by_design`` never appears here: it describes the tool-call level (activity records have no token field),
        which is not the same unit as a dispatch and never entered the unattributed dict to begin with (see the
        ``UnattributedReason`` docs).
        """
        stmt = select(
            AgentModel.id,
            AgentModel.name,
            AgentModel.project_id,
            AgentModel.transcript_path,
            AgentModel.created_at,
        ).select_from(AgentModel)
        stmt = self._dispatch_population_filter(stmt, population)
        stmt = _apply_created_window(stmt, since, until)
        stmt = (
            stmt.where(AgentModel.tokens_measured_at.is_(None))
            .order_by(AgentModel.created_at.desc())
            .limit(scan_limit)
        )

        async with get_session(self._db_url) as session:
            rows = (await session.execute(stmt)).all()

        samples: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            code = classify_unattributed(row.transcript_path)
            bucket = samples.setdefault(code, [])
            if len(bucket) >= per_reason:
                continue
            bucket.append(
                {
                    "agent_id": row.id,
                    "name": row.name,
                    "project_id": row.project_id or "",
                    "transcript_path": row.transcript_path or "",
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
        return {"scanned": len(rows), "scan_limit": scan_limit, "samples": samples}

    def _scope_group_key(self, scope: AttributionScope) -> Any:
        """Which column this level groups by — what "one row" of the drill-down candidate list is."""
        if scope is AttributionScope.PROJECT:
            return AgentModel.project_id
        if scope is AttributionScope.SESSION:
            return AgentModel.session_id
        if scope is AttributionScope.AGENT:
            return AgentModel.id
        if scope is AttributionScope.WORKFLOW_RUN:
            return WorkflowAgentModel.run_id
        return KnowledgeLinkModel.to_id  # TASK

    async def _scope_labels(self, scope: AttributionScope, keys: list[str]) -> dict[str, str]:
        """Pair candidate ids with human-readable names. When unavailable, leave it empty and let the presentation fall
        back to showing the id — never fabricate a name."""
        if not keys:
            return {}
        spec: dict[AttributionScope, tuple[Any, Any, Any]] = {
            AttributionScope.PROJECT: (ProjectModel, ProjectModel.id, ProjectModel.name),
            AttributionScope.AGENT: (AgentModel, AgentModel.id, AgentModel.name),
            # The workflow_run level's scope_id is ``workflow_agents.run_id``, which in practice stores
            # the **wf_id** (shaped like wf_3db2273a-91b), not workflow_runs' uuid primary key —
            # joining by primary key would match nothing at all, silently degrading to "no names anywhere".
            AttributionScope.WORKFLOW_RUN: (WorkflowRunModel, WorkflowRunModel.wf_id, WorkflowRunModel.name),
            AttributionScope.TASK: (TaskModel, TaskModel.id, TaskModel.title),
        }
        if scope not in spec:
            return {}  # SESSION: the id itself is its name; there is no second name to look up
        model, id_col, label_col = spec[scope]
        stmt = select(id_col, label_col).select_from(model).where(id_col.in_(keys))
        async with get_session(self._db_url) as session:
            return {row[0]: (row[1] or "") for row in (await session.execute(stmt)).all()}

    async def list_attribution_scopes(
        self,
        *,
        scope: AttributionScope,
        parent_scope: AttributionScope | None = None,
        parent_id: str = "",
        population: DispatchPopulation = DispatchPopulation.SUBAGENT,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Enumerate the drill-down candidates under one level — returns only dispatch counts and measured counts, **not
        a single token number**.

        §5.2 item 3 requires stepwise drill-down ``project → session → workflow_run → agent``, and "which options
        this level has" is itself a query. It is deliberately split from :meth:`aggregate_token_attribution`:
        the four-layer usage can only be obtained by issuing a separate attribution query for **the one selected**
        scope_id. If the list conveniently
        carried tokens along, it would give birth to a leaderboard without denominators — exactly what §2.5 exists to
        structurally eliminate.

        The numerator/denominator share the same pair of filter helpers as the aggregation query, so the card you see
        when opening a row has a denominator that
        necessarily equals that row's denominator in the list — two separately written SQLs would quietly diverge after
        some change.

        The sort key is the **dispatch count** (a count dimension), not usage: there is no usage here at all. The real
        usage sorting
        is borne by the card's ``output_tokens`` (§1.2).
        """
        if scope is AttributionScope.TASK and parent_id:
            # task is a side branch, not a fifth level (§2.1): it has no parent-child relation with the first four
            # levels,
            # and force-fitting a parent filter would yield a subset that looks reasonable but is actually inexplicable.
            msg = (
                "scope=task is a side branch of the attribution chain and does not accept parent_id — it is not "
                "the next level below project/session"
            )
            raise ValueError(msg)

        key = self._scope_group_key(scope)
        measured = AgentModel.tokens_measured_at.is_not(None)
        # TASK's "attributed" has one more gate, verbatim from the same origin as aggregate_token_attribution:
        # an agent that left bookkeeping entries across multiple tasks cannot be split; count it honestly as
        # unattributed, with no averaging allocation (§2.4).
        attributed = (
            and_(measured, AgentModel.id.in_(_single_worked_on_agents_subquery()))
            if scope is AttributionScope.TASK
            else measured
        )

        stmt = select(
            key.label("scope_id"),
            func.count(func.distinct(AgentModel.id)).label("total"),
            func.count(func.distinct(case((attributed, AgentModel.id)))).label("attributed"),
        ).select_from(AgentModel)
        # One agent can correspond to multiple workflow_agents rows / multiple task edges, so counting is uniformly
        # DISTINCT
        # agent id — counting joined rows would inflate the denominator and systematically depress coverage.
        if scope is AttributionScope.WORKFLOW_RUN:
            stmt = stmt.join(WorkflowAgentModel, WorkflowAgentModel.os_agent_id == AgentModel.id)
        elif scope is AttributionScope.TASK:
            stmt = stmt.join(
                KnowledgeLinkModel,
                and_(
                    KnowledgeLinkModel.from_id == AgentModel.id,
                    KnowledgeLinkModel.from_kind == AGENT_TASK_LINK_FROM_KIND,
                    KnowledgeLinkModel.to_kind == AGENT_TASK_LINK_TO_KIND,
                    KnowledgeLinkModel.link_type == AGENT_TASK_LINK_TYPE,
                ),
            )
        stmt = self._dispatch_population_filter(stmt, population)
        if parent_scope is not None and parent_id:
            stmt = self._attribution_scope_filter(stmt, parent_scope, parent_id)
        stmt = _apply_created_window(stmt, since, until)
        stmt = (
            stmt.where(key.is_not(None))
            .where(key != "")
            .group_by(key)
            # The ordering key gets a "most recent activity" fallback: at the agent level every row's dispatch count is
            # constantly 1, so sorting by count alone
            # yields primary-key lexicographic order — a list that looks ordered but is unrelated to anything anyone
            # cares about.
            .order_by(
                func.count(func.distinct(AgentModel.id)).desc(),
                func.max(AgentModel.created_at).desc(),
            )
            .limit(limit)
        )

        async with get_session(self._db_url) as session:
            rows = (await session.execute(stmt)).all()

        labels = await self._scope_labels(scope, [r.scope_id for r in rows])
        return [
            {
                "scope": scope.value,
                "scope_id": r.scope_id,
                "label": labels.get(r.scope_id, ""),
                "dispatches_total": int(r.total or 0),
                "dispatches_attributed": int(r.attributed or 0),
            }
            for r in rows
        ]
