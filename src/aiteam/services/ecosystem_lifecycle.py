"""Ecosystem lifecycle trigger service (v1.5.0-C).

Implements the Stage 1/2/3 funnel trigger primitives described in
``docs/v1.5.0-progressive-deep-review-design.md`` §2.2 / §2.3 / §2.4.

Design principle (user decision A): **the ecosystem does not redo workflows; it only triggers the existing systems**.
- Stage 2 debate → goes through the existing meeting/debate_start (this service returns the meeting payload,
  and the MCP tool/Leader calls ``debate_start``).
- Stage 3 integration → goes through the existing task_create (this service returns the task payload, and the MCP tool
  calls ``/api/projects/{id}/tasks``).

Service operations
------------------
1. ``request_deep_review_batch`` — Stage 1 launch: pulls candidates by tags → creates for each repo
   an ``EcosystemDeepReview`` row + advances stage_status=QUEUED → returns the backend-architect
   sub-agent's ``DispatchIntent`` list (the architecture_md write-back path).
2. ``apply_architecture_md`` — after the agent writes back the architecture markdown, advances
   ``stage_status=architecture_done`` and records ``architecture_completed_at``.
3. ``trigger_debate`` — Stage 2 launch: candidate reviews must be at architecture_done.
   Returns a ``DebateDispatchIntent`` (including the debate_start parameters + the associated review_ids),
   and after the MCP tool/Leader calls ``debate_start``, ``link_debate_meeting`` writes it back.
4. ``link_debate_meeting`` — Associates the meeting id to all related reviews, so hooks can write back in reverse.
5. ``apply_debate_result`` — Stage 2 conclusion write-back: after the agent writes risks/learnings/integration,
   advances ``stage_status=debated``.
6. ``mark_as_reference`` — Stage 3 reference path: adds the lifecycle:reference tag +
   advances ``stage_status=referenced``.
7. ``start_integration`` — Stage 3 integrate path: returns a ``TaskDispatchIntent``
   (task_create parameters); after the MCP tool calls ``/api/projects/{id}/tasks``, it then calls
   ``link_integration_task`` to write back.

Dispatch pattern
----------------
The Service does not spawn agents / create meetings / create tasks; instead it returns *Intent* dataclasses
for the upper layer (MCP tools or the Leader) to actually trigger, consistent with the Stage 0 worker
(``DispatchIntent``),
which facilitates unit testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemStageStatus,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

# Stage 1 timeout per design §3.1.
STAGE1_TIMEOUT_SECONDS = 1800  # 30 min

# Lifecycle seed tag names (design draft §4.5 — 5 lifecycle tags)
LIFECYCLE_TAG_REFERENCE = "reference"
LIFECYCLE_TAG_INTEGRATED = "integrated"
LIFECYCLE_TAG_EVALUATING = "evaluating"
LIFECYCLE_TAG_DELETED = "deleted"
LIFECYCLE_TAG_PRIVATE_NOW = "private_now"

LIFECYCLE_TAGS: tuple[str, ...] = (
    LIFECYCLE_TAG_REFERENCE,
    LIFECYCLE_TAG_INTEGRATED,
    LIFECYCLE_TAG_EVALUATING,
    LIFECYCLE_TAG_DELETED,
    LIFECYCLE_TAG_PRIVATE_NOW,
)

# Lifecycle tag descriptions for ensure-on-demand seeding.
_LIFECYCLE_TAG_DESCRIPTIONS: dict[str, str] = {
    LIFECYCLE_TAG_REFERENCE: "Used by the project as an architecture reference",
    LIFECYCLE_TAG_INTEGRATED: "Integrated and implemented by the project",
    LIFECYCLE_TAG_EVALUATING: "Under evaluation debate",
    LIFECYCLE_TAG_DELETED: "Deleted on the GitHub side",
    LIFECYCLE_TAG_PRIVATE_NOW: "Made private on the GitHub side",
}

# Keywords that hint a meeting is ecosystem-related (used by the reverse-write hook).
ECOSYSTEM_KEYWORDS: tuple[str, ...] = (
    "ecosystem",
    "ecosystem library",
    "ecosystem repo",
    "deep review",
    "deep scan",
    "deep_review",
)


# ============================================================
# Stage 1 architecture-analysis sub-agent prompt
# ============================================================

ARCHITECTURE_AGENT_PROMPT = """\
You are an ecosystem Stage 1 architecture-analysis sub-agent (backend-architect).

## Target repo
- repo_full_name: {repo_full_name}
- repo_id: {repo_id}
- deep_review_id: {deep_review_id}
- stars: {stars}
- shallow_summary:
{shallow_summary}

## Research goal
{research_goal}

## Task (800-1500 words of English markdown)
git clone --depth=1 https://github.com/{repo_full_name} /tmp/ecosystem-arch/{repo_id}
Analyze the repo's architecture key points and answer:
1. **Architecture overview** — the core layering, module division, and where the entry points are
2. **Key file list** — the 5-10 files most worth studying + a one-sentence note for each
3. **Core modules** — each core module's responsibilities, external interfaces, and dependencies
4. **Integration entry points** — if we want to learn from / integrate it, where to start with the least risk

## On completion, call
ecosystem_apply_architecture_md(
    deep_review_id="{deep_review_id}",
    architecture_md="<800-1500 words of English markdown>",
)

## Constraints
- Use a --depth=1 shallow clone to save space.
- Do not commit / push that repo's code into our project.
- When you cannot complete it (clone failure / encoding error): when calling ecosystem_apply_architecture_md,
  leave architecture_md empty and add an error_message field; the OS will take the failed classification.
"""


# ============================================================
# Result types — Intent dataclasses
# ============================================================


@dataclass
class DeepReviewBatchIntent:
    """Stage 1 batch dispatch intent.

    Each entry corresponds to one candidate repo's backend-architect agent dispatch.
    """

    repo_id: str
    repo_full_name: str
    deep_review_id: str
    prompt: str
    timeout_seconds: int = STAGE1_TIMEOUT_SECONDS
    project_id: str | None = None


@dataclass
class DebateDispatchIntent:
    """Stage 2 debate dispatch intent.

    Returned by ``trigger_debate`` so the caller (MCP tool/Leader) can call the existing
    ``debate_start`` MCP tool or ``meeting_create``. Includes suggested participants and a discussion topic
    template, but the concrete roles/participants are decided by the Leader at call time.
    """

    review_ids: list[str]
    repo_full_names: list[str]
    research_goal: str
    suggested_topic: str
    suggested_advocate: str = "backend-architect"
    suggested_critic: str = "code-reviewer"
    suggested_judge: str = "team-lead"
    project_id: str | None = None


@dataclass
class TaskDispatchIntent:
    """Stage 3 integration task dispatch intent.

    Returned by ``start_integration`` so the caller can POST to
    ``/api/projects/{project_id}/tasks``. After the upper layer creates the task, it calls
    ``link_integration_task`` to write task.id back to the review.
    """

    review_id: str
    repo_id: str
    repo_full_name: str
    title: str
    description: str
    priority: str = "high"
    horizon: str = "mid"
    tags: list[str] = field(default_factory=list)
    project_id: str | None = None


@dataclass
class WritebackHint:
    """Reverse-writeback hint emitted when a meeting topic looks ecosystem-related.

    The hook (``hooks/meeting_ecosystem_writeback.py``) reads concluded meeting
    topics and, when they hint at an ecosystem debate, emits this so Leader
    knows which review_ids should receive ``apply_debate_result`` calls.
    """

    meeting_id: str
    topic: str
    matched_keywords: list[str]
    review_ids: list[str]


# ============================================================
# Service
# ============================================================


class EcosystemLifecycleService:
    """Stage 1/2/3 trigger primitives + agent writeback handlers."""

    def __init__(
        self,
        repo: StorageRepository,
        *,
        project_id: str = "",
    ) -> None:
        """Initialize the lifecycle service.

        Args:
            repo: the data access layer.
            project_id: explicit project scope; when empty, falls back to ``repo._project_scope``.
                All write-back operations are limited to that project.
        """
        self._repo = repo
        self._project_id = project_id or repo._project_scope or ""

    # ------------------------------------------------------------------
    # Stage 1 — request_deep_review_batch + apply_architecture_md
    # ------------------------------------------------------------------

    async def request_deep_review_batch(
        self,
        *,
        tags: list[str] | None = None,
        min_stars: int | None = None,
        limit: int = 20,
        research_goal: str = "",
    ) -> list[DeepReviewBatchIntent]:
        """Stage 1 candidate dispatch: pull repos that are active+shallow_done+tags → create review rows.

        Args:
            tags: the required candidate-filter tag list (AND semantics). Empty is an error.
            min_stars: the project setting's min_stars default; an explicit value overrides it.
            limit: how many candidates to pull at most (default 20).
            research_goal: the research-goal phrase, injected into the sub-agent prompt.

        Returns:
            One ``DeepReviewBatchIntent`` per candidate repo; the caller spawns from these
            backend-architect agent.

        Raises:
            ValueError: when tags is empty.
        """
        if not tags:
            raise ValueError("tags must not be empty: Stage 1 must filter candidates by tags")

        # Resolve the min_stars default (project settings)
        if min_stars is None and self._project_id:
            settings = await self._repo.get_ecosystem_project_settings(
                self._project_id
            )
            if settings is not None:
                min_stars = settings.min_stars
        if min_stars is None:
            min_stars = 1000

        # Pull candidates — must be active + shallow_done + contain all tags
        profiles, _ = await self._repo.search_ecosystem_profiles_extended(
            min_stars=min_stars,
            tags=list(tags),
            tag_match_mode="all",
            limit=limit,
            offset=0,
            project_id=self._project_id or None,
        )

        intents: list[DeepReviewBatchIntent] = []
        for p in profiles:
            if not p.is_active or p.is_deleted or p.is_private_now:
                continue
            # A shallow_summary is required to enter Stage 1
            if not p.shallow_summary:
                continue
            # Skip when the same repo already has an unfinished architecture review
            existing = await self._repo.list_deep_reviews_by_stage(
                EcosystemStageStatus.ARCHITECTURE_DONE,
                repo_id=p.id,
                project_id=self._project_id or None,
            )
            if existing:
                continue
            # D5 convergence: the in-flight criterion looks only at the authoritative axis stage_status (status has
            # stopped being written as running,
            # and the old status∈{QUEUED,RUNNING} condition being constantly false for new rows would cause duplicate
            # dispatches).
            # Two in-flight shapes:
            #   1) stage=queued — the Stage 0 shallow scan is still in flight;
            #   2) stage=shallow_done with architecture_md empty and claimed_by non-empty
            #      — a Stage 1 architecture review dispatched earlier by this method is in flight (the row carries
            #      claimed_by from creation; the choke point releases it automatically on stage advancement).
            in_flight = False
            current = await self._repo.list_deep_reviews(
                repo_id=p.id, project_id=self._project_id or None
            )
            for row in current:
                if row.stage_status == EcosystemStageStatus.QUEUED:
                    in_flight = True
                    break
                if (
                    row.stage_status == EcosystemStageStatus.SHALLOW_DONE
                    and row.architecture_md == ""
                    and row.claimed_by
                ):
                    in_flight = True
                    break
            if in_flight:
                continue

            # D5: do not pass status (create_deep_review derives it from the stage as completed,
            # consistent with the backfill F1 mapping); the row atomically carries claimed_by to express Stage 1 in
            # flight,
            # simultaneously plugging claim_next_review_repo's double claim on in-flight rows.
            now = utc_now()
            review = EcosystemDeepReview(
                project_id=self._project_id or None,
                repo_id=p.id,
                stage_status=EcosystemStageStatus.SHALLOW_DONE,
                claimed_at=now,
            )
            review.claimed_by = f"lifecycle:{review.id[:8]}"
            await self._repo.create_deep_review(
                review, project_id=self._project_id or None
            )

            prompt = self._build_architecture_prompt(
                profile=p,
                deep_review_id=review.id,
                research_goal=research_goal,
            )
            # D5: stop writing status=RUNNING — status is a derived read-only view.
            await self._repo.update_deep_review(
                review.id,
                _project_id=self._project_id or None,
                started_at=now,
                dispatch_prompt=prompt,
            )

            intents.append(
                DeepReviewBatchIntent(
                    repo_id=p.id,
                    repo_full_name=p.repo_full_name,
                    deep_review_id=review.id,
                    prompt=prompt,
                    project_id=self._project_id or None,
                )
            )

        logger.info(
            "ecosystem_lifecycle.request_deep_review_batch tags=%s dispatched=%d",
            tags,
            len(intents),
        )
        return intents

    async def apply_architecture_md(
        self,
        deep_review_id: str,
        *,
        architecture_md: str,
        agent_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Stage 1 write-back: the agent submits architecture_md → advances to architecture_done.

        Args:
            deep_review_id: the target review id.
            architecture_md: 800-1500 words of English markdown. An empty string triggers an error.
            agent_id: optional — written into review.agent_id for traceability.

        Returns:
            The updated review; not found → None.

        Raises:
            ValueError: when architecture_md is empty.
        """
        if not architecture_md or not architecture_md.strip():
            raise ValueError("architecture_md must not be empty")

        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        # D5: no longer side-writes status=COMPLETED / completed_at — the subsequent
        # update_deep_review_stage(ARCHITECTURE_DONE) derives and fills them in.
        update_kwargs: dict = {
            "_project_id": self._project_id or None,
            "architecture_md": architecture_md.strip(),
        }
        if agent_id:
            update_kwargs["agent_id"] = agent_id

        await self._repo.update_deep_review(deep_review_id, **update_kwargs)
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.ARCHITECTURE_DONE,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Stage 2 — trigger_debate / link_debate_meeting / apply_debate_result
    # ------------------------------------------------------------------

    async def trigger_debate(
        self,
        *,
        repo_ids: list[str],
        research_goal: str,
        suggested_advocate: str = "backend-architect",
        suggested_critic: str = "code-reviewer",
        suggested_judge: str = "team-lead",
    ) -> DebateDispatchIntent:
        """Stage 2 trigger: pull the architecture_done reviews and generate the debate dispatch payload.

        Args:
            repo_ids: the selected finalist repo id list (1-5 is appropriate).
            research_goal: the research goal, used for the meeting topic.
            suggested_advocate / critic / judge: recommended roles; the caller may override them.

        Returns:
            A ``DebateDispatchIntent``; the caller uses it to call the ``debate_start`` MCP tool
            or POST directly to ``/api/teams/{team}/meetings``.

        Raises:
            ValueError: repo_ids is empty, or there is no corresponding architecture_done review.
        """
        if not repo_ids:
            raise ValueError("repo_ids must not be empty: Stage 2 must specify finalists")

        review_ids: list[str] = []
        repo_full_names: list[str] = []
        for repo_id in repo_ids:
            reviews = await self._repo.list_deep_reviews_by_stage(
                EcosystemStageStatus.ARCHITECTURE_DONE,
                repo_id=repo_id,
                project_id=self._project_id or None,
            )
            if not reviews:
                logger.warning(
                    "ecosystem_lifecycle.trigger_debate skipping repo_id=%s "
                    "(no architecture_done review)",
                    repo_id,
                )
                continue
            # Take the latest architecture_done review
            review = reviews[0]
            review_ids.append(review.id)
            profile = await self._repo.get_ecosystem_profile_by_id(
                repo_id, project_id=self._project_id or None
            )
            if profile is not None:
                repo_full_names.append(profile.repo_full_name)
            else:
                repo_full_names.append(f"<unknown:{repo_id[:8]}>")

        if not review_ids:
            raise ValueError(
                "No repo_ids correspond to an architecture_done review; "
                "cannot trigger a debate. Please complete Stage 1 first."
            )

        topic = self._build_debate_topic(
            repo_full_names=repo_full_names, research_goal=research_goal
        )
        return DebateDispatchIntent(
            review_ids=review_ids,
            repo_full_names=repo_full_names,
            research_goal=research_goal,
            suggested_topic=topic,
            suggested_advocate=suggested_advocate,
            suggested_critic=suggested_critic,
            suggested_judge=suggested_judge,
            project_id=self._project_id or None,
        )

    async def link_debate_meeting(
        self,
        *,
        review_ids: list[str],
        meeting_id: str,
    ) -> int:
        """After the meeting is created, write meeting_id back to all related review rows.

        Pairs with trigger_debate: trigger_debate returns review_ids → the caller calls
        debate_start to create the meeting → passes meeting.id + review_ids together to this method.

        Args:
            review_ids: the associated review id list.
            meeting_id: the created meeting id.

        Returns:
            The number of review rows actually updated.
        """
        if not review_ids or not meeting_id:
            return 0
        updated = 0
        for review_id in review_ids:
            row = await self._repo.update_deep_review(
                review_id,
                _project_id=self._project_id or None,
                debate_meeting_id=meeting_id,
            )
            if row is not None:
                updated += 1
        return updated

    async def apply_debate_result(
        self,
        deep_review_id: str,
        *,
        risks_md: str = "",
        learnings_md: str = "",
        integration_md: str = "",
        integration_recommendation: str = "",
        agent_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Stage 2 write-back: debate conclusion → advances stage_status=debated.

        Args:
            deep_review_id: the target review id.
            risks_md: the risk-points markdown.
            learnings_md: the learnings markdown.
            integration_md: the integration-suggestion markdown.
            integration_recommendation: an enum string (integrate/reference/learn/skip).
            agent_id: optional — written into review.agent_id.

        Returns:
            The updated review; not found → None.

        Raises:
            ValueError: all three *_md are empty (at least one non-empty counts as a valid conclusion).
        """
        non_empty = [s for s in (risks_md, learnings_md, integration_md) if s.strip()]
        if not non_empty:
            raise ValueError(
                "At least one of risks_md / learnings_md / integration_md must be non-empty"
            )

        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        update_kwargs: dict = {
            "_project_id": self._project_id or None,
        }
        if risks_md.strip():
            update_kwargs["risks_md"] = risks_md.strip()
        if learnings_md.strip():
            update_kwargs["learnings_md"] = learnings_md.strip()
        if integration_md.strip():
            update_kwargs["integration_md"] = integration_md.strip()
        if agent_id:
            update_kwargs["agent_id"] = agent_id

        # Handle integration_recommendation
        from aiteam.types import IntegrationRecommendation

        if integration_recommendation:
            try:
                update_kwargs["integration_recommendation"] = (
                    IntegrationRecommendation(integration_recommendation.lower())
                )
            except ValueError:
                logger.warning(
                    "apply_debate_result: invalid integration_recommendation=%r",
                    integration_recommendation,
                )

        await self._repo.update_deep_review(deep_review_id, **update_kwargs)
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.DEBATED,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Stage 3 — mark_as_reference / start_integration
    # ------------------------------------------------------------------

    async def mark_as_reference(
        self,
        deep_review_id: str,
        *,
        agent_id: str | None = None,
        confidence: float = 1.0,
    ) -> EcosystemDeepReview | None:
        """Stage 3 reference: adds the lifecycle:reference tag + advances to referenced.

        Args:
            deep_review_id: the target review id.
            agent_id: optional — written into EcosystemRepoTag.agent_id.
            confidence: the tag confidence (default 1.0 — a manual decision).

        Returns:
            The updated review; not found → None.
        """
        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        await self._ensure_lifecycle_tag_applied(
            repo_id=review.repo_id,
            tag_name=LIFECYCLE_TAG_REFERENCE,
            agent_id=agent_id,
            confidence=confidence,
        )
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.REFERENCED,
            project_id=self._project_id or None,
        )

    async def start_integration(
        self,
        deep_review_id: str,
        *,
        title: str = "",
        description: str = "",
        priority: str = "high",
        horizon: str = "mid",
        extra_tags: list[str] | None = None,
    ) -> TaskDispatchIntent:
        """Stage 3 integrate: returns the task_create payload; the caller actually creates the task.

        This method only builds the ``TaskDispatchIntent`` (it does not call task_create directly) and advances
        review.stage_status=INTEGRATED. The caller is responsible for calling ``/api/projects/{id}/tasks``
        to create the task, then calling ``link_integration_task`` to write task.id back to the review.

        Args:
            deep_review_id: the target review id.
            title: the task title; when empty, auto-generated as ``Integrate {repo_full_name}: {summary}``.
            description: the task description; when empty, auto-assembled from the review's architecture/integration_md.
            priority / horizon: the task priority and time horizon.
            extra_tags: additional tags (``ecosystem-integration`` is appended by default).

        Returns:
            A ``TaskDispatchIntent``; the caller POSTs its fields to the task API.

        Raises:
            ValueError: the review does not exist or has not reached the debated state.
        """
        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            raise ValueError(f"deep_review id={deep_review_id} not found")
        if review.stage_status not in (
            EcosystemStageStatus.DEBATED,
            EcosystemStageStatus.ARCHITECTURE_DONE,
            EcosystemStageStatus.REFERENCED,
        ):
            raise ValueError(
                f"deep_review stage_status={review.stage_status.value}, "
                "It must first reach architecture_done / debated / referenced before integration"
            )

        profile = await self._repo.get_ecosystem_profile_by_id(
            review.repo_id, project_id=self._project_id or None
        )
        if profile is None:
            raise ValueError(f"repo profile id={review.repo_id} not found")

        # Mark the lifecycle:integrated tag
        await self._ensure_lifecycle_tag_applied(
            repo_id=review.repo_id,
            tag_name=LIFECYCLE_TAG_INTEGRATED,
            agent_id=None,
            confidence=1.0,
        )

        # Build the task payload
        repo_label = profile.repo_full_name
        if not title:
            short_summary = (
                profile.shallow_summary or profile.description or ""
            ).strip()[:60]
            title = f"Integrate {repo_label}: {short_summary}".strip()
        if not description:
            description = self._build_integration_description(profile, review)

        tags = ["ecosystem-integration", f"repo:{repo_label}"]
        if extra_tags:
            tags.extend(t for t in extra_tags if t and t not in tags)

        # Advance stage_status → integrated (task_id is written later by link_integration_task)
        await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.INTEGRATED,
            project_id=self._project_id or None,
        )

        return TaskDispatchIntent(
            review_id=deep_review_id,
            repo_id=review.repo_id,
            repo_full_name=repo_label,
            title=title,
            description=description,
            priority=priority,
            horizon=horizon,
            tags=tags,
            project_id=self._project_id or None,
        )

    async def link_integration_task(
        self,
        *,
        deep_review_id: str,
        task_id: str,
    ) -> EcosystemDeepReview | None:
        """Pairs with start_integration: after the task is created, writes back task_id.

        Args:
            deep_review_id: the target review id.
            task_id: the created task id.

        Returns:
            The updated review; not found → None.
        """
        if not task_id:
            return None
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.INTEGRATED,
            integration_task_id=task_id,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Reverse-writeback hint detection
    # ------------------------------------------------------------------

    async def detect_meeting_writeback(
        self,
        *,
        meeting_id: str,
        topic: str,
    ) -> WritebackHint | None:
        """When a meeting ends, detect whether it involves the ecosystem library → remind the Leader to write back in
        reverse.

        If the topic contains any ECOSYSTEM_KEYWORDS keyword → pull all reviews with debate_meeting_id=
        meeting_id and return a ``WritebackHint`` so the hook/Leader
        knows to have an agent call apply_debate_result.

        Args:
            meeting_id: the just-ended meeting id.
            topic: the meeting topic text.

        Returns:
            A ``WritebackHint`` or None (if the topic hits no keyword).
        """
        if not topic:
            return None
        topic_lower = topic.lower().replace("-", " ")
        matched = [kw for kw in ECOSYSTEM_KEYWORDS if kw.lower() in topic_lower]
        if not matched:
            return None

        # Pull all reviews associated with this meeting
        reviews = await self._repo.list_deep_reviews(
            limit=100, project_id=self._project_id or None
        )
        review_ids = [r.id for r in reviews if r.debate_meeting_id == meeting_id]

        return WritebackHint(
            meeting_id=meeting_id,
            topic=topic,
            matched_keywords=matched,
            review_ids=review_ids,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_architecture_prompt(
        *,
        profile: EcosystemRepoProfile,
        deep_review_id: str,
        research_goal: str,
    ) -> str:
        """Render the Stage 1 sub-agent prompt."""
        return ARCHITECTURE_AGENT_PROMPT.format(
            repo_full_name=profile.repo_full_name,
            repo_id=profile.id,
            deep_review_id=deep_review_id,
            stars=profile.stars,
            shallow_summary=(profile.shallow_summary or "(no shallow-scan summary yet)")[:1500],
            research_goal=research_goal or "(no specific research goal specified)",
        )

    @staticmethod
    def _build_debate_topic(
        *,
        repo_full_names: list[str],
        research_goal: str,
    ) -> str:
        """Assemble the meeting topic string."""
        head = research_goal or "Ecosystem library selection debate"
        candidates = ", ".join(repo_full_names[:5])
        if len(repo_full_names) > 5:
            candidates += f"... ({len(repo_full_names)} in total)"
        return f"[Ecosystem Stage 2] {head} — Candidates: {candidates}"

    @staticmethod
    def _build_integration_description(
        profile: EcosystemRepoProfile,
        review: EcosystemDeepReview,
    ) -> str:
        """Assemble the integration task description (summarizing the architecture / integration md)."""
        parts: list[str] = [
            "## Integration goal",
            f"Integrate [{profile.repo_full_name}](https://github.com/{profile.repo_full_name}) "
            "into this project.",
            "",
        ]
        if profile.shallow_summary:
            parts.append("## Repo overview (shallow scan)")
            parts.append(profile.shallow_summary[:500])
            parts.append("")
        if review.architecture_md:
            parts.append("## Architecture overview")
            parts.append(review.architecture_md[:1500])
            parts.append("")
        if review.integration_md:
            parts.append("## Integration suggestions (from the Stage 2 debate)")
            parts.append(review.integration_md[:1500])
            parts.append("")
        if review.risks_md:
            parts.append("## Known risks")
            parts.append(review.risks_md[:800])
            parts.append("")
        parts.append("## Links")
        parts.append(f"- ecosystem deep_review_id: `{review.id}`")
        parts.append(f"- ecosystem repo_id: `{review.repo_id}`")
        if review.debate_meeting_id:
            parts.append(f"- Debate meeting: `{review.debate_meeting_id}`")
        return "\n".join(parts)

    async def _ensure_lifecycle_tag(self, tag_name: str) -> EcosystemTag:
        """Seed a lifecycle tag on demand (the positioning class). Returns if it already exists."""
        existing = await self._repo.get_tag_by_name(tag_name)
        if existing is not None:
            return existing
        new_tag = EcosystemTag(
            name=tag_name,
            category=EcosystemTagCategory.POSITIONING,
            description=_LIFECYCLE_TAG_DESCRIPTIONS.get(tag_name, ""),
        )
        await self._repo.upsert_tag(new_tag)
        seeded = await self._repo.get_tag_by_name(tag_name)
        return seeded or new_tag

    async def _ensure_lifecycle_tag_applied(
        self,
        *,
        repo_id: str,
        tag_name: str,
        agent_id: str | None,
        confidence: float,
    ) -> EcosystemRepoTag:
        """Add a lifecycle:* tag to a repo (if it exists, update source/agent/confidence)."""
        tag = await self._ensure_lifecycle_tag(tag_name)
        repo_tag = EcosystemRepoTag(
            project_id=self._project_id or None,
            repo_id=repo_id,
            tag_id=tag.id,
            confidence=max(0.0, min(1.0, confidence)),
            source=EcosystemTagSource.LIFECYCLE,
            agent_id=agent_id,
        )
        return await self._repo.add_repo_tag(
            repo_tag, project_id=self._project_id or None
        )


__all__ = [
    "EcosystemLifecycleService",
    "DeepReviewBatchIntent",
    "DebateDispatchIntent",
    "TaskDispatchIntent",
    "WritebackHint",
    "ARCHITECTURE_AGENT_PROMPT",
    "STAGE1_TIMEOUT_SECONDS",
    "LIFECYCLE_TAG_REFERENCE",
    "LIFECYCLE_TAG_INTEGRATED",
    "LIFECYCLE_TAG_EVALUATING",
    "LIFECYCLE_TAG_DELETED",
    "LIFECYCLE_TAG_PRIVATE_NOW",
    "LIFECYCLE_TAGS",
    "ECOSYSTEM_KEYWORDS",
]
