"""Failure alchemy — distill defense rules, training cases, and improvement proposals from failures."""

from __future__ import annotations

import logging
from typing import Any

from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

logger = logging.getLogger(__name__)


class FailureAlchemist:
    """Failure alchemist — transform failed tasks into three learning artifacts.

    Artifacts:
    - Antibody: Defense rule suggestions to prevent similar failures
    - Vaccine: Structured failure cases for new agent onboarding
    - Catalyst: System improvement proposals to drive process optimization
    """

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def process_failure(self, task_id: str, team_id: str, template_name: str = "") -> dict:
        """Process a failed task and distill three learning artifacts, saving them to team memory.

        Args:
            task_id: ID of the failed task.
            team_id: ID of the owning team.

        Returns:
            Dict containing antibody, vaccine, and catalyst artifacts;
            returns {"error": "task not found"} if the task does not exist.
        """
        task = await self._repo.get_task(task_id)
        if not task:
            logger.warning("FailureAlchemist: task %s not found", task_id)
            return {"error": "task not found"}

        antibody = self._generate_antibody(task)
        vaccine = self._generate_vaccine(task)
        catalyst = self._generate_catalyst(task)

        failure_meta: dict = {
            "type": "failure_alchemy",
            "task_id": task_id,
            "task_title": task.title,
            "antibody": antibody,
            "vaccine": vaccine,
            "catalyst": catalyst,
            "created_at": utc_now().isoformat(),
        }
        # Associate with agent template if provided — enables prompt effectiveness tracking
        if template_name:
            failure_meta["template_name"] = template_name

        # Ownership level: project rather than team (2026-07-27 ruling). Under CC v2.1.219 a team is
        # a session-level / single-workflow-level short-lived container — once a team closes, the failure lessons filed
        # under its name
        # can no longer be read by anyone; measured on this machine, 125 such orphans already exist. The project is the
        # level where lessons should truly settle
        # (the next team and the next workflow of the same project should all be able to read them).
        # The historical 125 entries are **not migrated** (kept as a team-layer archive, avoiding rewriting the
        # ownership of historical rows).
        # When no project ownership can be obtained (no team, or the team is not bound to a project), fall back to the
        # team layer; never drop a record.
        scope, scope_id = "team", team_id
        try:
            team = await self._repo.get_team(team_id)
            project_id = getattr(team, "project_id", None) if team else None
            if project_id:
                scope, scope_id = "project", project_id
                failure_meta["team_id"] = team_id
        except Exception:
            logger.debug("FailureAlchemist: project ownership resolution failed; the memory falls back to the team "
                         "layer")

        await self._repo.create_memory(
            scope=scope,
            scope_id=scope_id,
            content=(
                f"Failure analysis: {task.title}\n\n"
                f"Antibody: {antibody}\n\n"
                f"Vaccine: {vaccine}\n\n"
                f"Catalyst: {catalyst}"
            ),
            metadata=failure_meta,
        )

        logger.info("FailureAlchemist: failed task '%s' has been distilled into a learning artifact", task.title)

        # Emitted here rather than in the route handler so direct callers are
        # covered too.  Best-effort: a bookkeeping miss must never sink the
        # analysis the caller actually asked for.
        try:
            await self._repo.create_event(
                event_type=EventType.TASK_FAILURE_ANALYZED.value,
                source="failure_alchemist",
                data={
                    "task_id": task_id,
                    "task_title": task.title,
                    "team_id": team_id,
                    "scope": scope,
                    "scope_id": scope_id,
                    "artifacts": ["antibody", "vaccine", "catalyst"],
                },
                entity_id=task_id,
                entity_type="task",
            )
        except Exception:
            logger.debug("FailureAlchemist: persisting the failure-alchemy event failed (does not affect the analysis "
                         "result)")

        return {"antibody": antibody, "vaccine": vaccine, "catalyst": catalyst}

    def _generate_antibody(self, task) -> str:
        """Extract defense rule suggestions from a failure."""
        result = task.result or ""
        error_info = task.config.get("error", "") if isinstance(task.config, dict) else ""
        failure_context = result or error_info or "No failure reason recorded"

        return (
            f"Defense rule suggestion: task '{task.title}' failed.\n"
            f"Failure reason: {failure_context[:200]}\n"
            f"Suggestion: check the relevant prerequisites before similar tasks start"
        )

    def _generate_vaccine(self, task) -> str:
        """Generate a structured failure case for new agent onboarding."""
        description = task.description[:150] if task.description else "None"
        result_summary = (task.result or "Not recorded")[:200]
        prevention = (
            task.config.get("error", "Check the prerequisites")
            if isinstance(task.config, dict)
            else "Check the prerequisites"
        )

        return (
            f"## Failure case: {task.title}\n"
            f"- Task description: {description}\n"
            f"- Assigned to: {task.assigned_to or 'Unassigned'}\n"
            f"- Failure result: {result_summary}\n"
            f"- Lesson: before executing this kind of task, confirm the environment and dependencies are ready first\n"
            f"- Prevention: {prevention}"
        )

    def _generate_catalyst(self, task) -> str:
        """Generate a system improvement proposal."""
        tags = task.tags if task.tags else []
        domain = ", ".join(tags) if tags else "General"

        return (
            f"Improvement proposal: '{task.title}' failure analysis\n"
            f"- Domains involved: {domain}\n"
            f"- Suggestions:\n"
            f"  1) Check the prerequisite checklist for this kind of task\n"
            f"  2) Add relevant automated tests\n"
            f"  3) Consider adding a Watchdog detection rule"
        )

    async def diagnose_failure(self, task_id: str) -> dict[str, Any]:
        """Auto-diagnose why a task failed by analyzing execution trace and memos.

        Reads the task's memo history to identify the failure point, compares with
        similar successful tasks in the same team, and generates actionable fix suggestions.

        Args:
            task_id: ID of the failed (or any) task to diagnose.

        Returns:
            Diagnosis report with root_cause, failed_at, similar_successes count,
            suggested_fixes, and rollback_recommendation.
        """
        task = await self._repo.get_task(task_id)
        if not task:
            logger.warning("FailureAlchemist.diagnose_failure: task %s not found", task_id)
            return {"error": "task not found"}

        # Extract memo trace
        memos: list[dict] = task.config.get("memo", []) if isinstance(task.config, dict) else []

        # Identify failure point — last memo with type "issue" or containing error keywords
        failed_at = "Not recorded"
        error_memo: dict | None = None
        error_keywords = ("\u5931\u8d25", "error", "exception", "failed", "blocked", "\u8d85\u65f6", "timeout")
        for memo in reversed(memos):
            content_lower = (memo.get("content", "") or "").lower()
            if memo.get("type") == "issue" or any(kw in content_lower for kw in error_keywords):
                error_memo = memo
                failed_at = memo.get("timestamp", "Unknown time")
                break

        # Determine root cause from task result / error config / memos
        result_text = task.result or ""
        config_error = task.config.get("error", "") if isinstance(task.config, dict) else ""
        memo_error = (error_memo.get("content", "") if error_memo else "")
        root_cause_raw = result_text or config_error or memo_error or "No failure reason recorded"
        root_cause = root_cause_raw[:300]

        # Count similar successful tasks in the same team (keyword overlap >= 2 in title)
        similar_successes = 0
        if task.team_id:
            from aiteam.types import TaskStatus

            all_tasks = await self._repo.list_tasks(task.team_id, status=TaskStatus.COMPLETED)
            task_words = set((task.title or "").lower().split())
            for t in all_tasks:
                if t.id == task_id:
                    continue
                other_words = set((t.title or "").lower().split())
                if len(task_words & other_words) >= 2:
                    similar_successes += 1

        # Generate fix suggestions based on failure context
        suggested_fixes = _generate_fix_suggestions(task, root_cause, memos)

        # Rollback recommendation
        rollback_recommendation = _build_rollback_recommendation(task)

        logger.info(
            "FailureAlchemist.diagnose_failure: task '%s' diagnosis complete, root cause: %s",
            task.title,
            root_cause[:80],
        )

        diagnosis = {
            "task_id": task_id,
            "task_title": task.title,
            "root_cause": root_cause,
            "failed_at": failed_at,
            "similar_successes": similar_successes,
            "suggested_fixes": suggested_fixes,
            "rollback_recommendation": rollback_recommendation,
        }

        # Same rationale as process_failure: the diagnosis has to leave a trace
        # in the event stream, otherwise "was this failure ever looked at?" is
        # unanswerable after the fact.
        try:
            await self._repo.create_event(
                event_type=EventType.TASK_FAILURE_DIAGNOSED.value,
                source="failure_alchemist",
                data={
                    "task_id": task_id,
                    "task_title": task.title,
                    "team_id": task.team_id,
                    "root_cause": root_cause,
                    "failed_at": failed_at,
                    "similar_successes": similar_successes,
                    "suggested_fix_count": len(suggested_fixes),
                },
                entity_id=task_id,
                entity_type="task",
            )
        except Exception:
            logger.debug("FailureAlchemist: persisting the failure-diagnosis event failed (does not affect the "
                         "diagnosis result)")

        return diagnosis


def _generate_fix_suggestions(task: Any, root_cause: str, memos: list[dict]) -> list[str]:
    """Generate actionable fix suggestions based on failure context."""
    fixes: list[str] = []
    root_lower = root_cause.lower()

    # Dependency / blocked failures
    if "blocked" in root_lower or "\u4f9d\u8d56" in root_lower or "depend" in root_lower:
        fixes.append("Check and complete all prerequisite dependency tasks before retrying")

    # Timeout failures
    if "timeout" in root_lower or "\u8d85\u65f6" in root_lower or "timed out" in root_lower:
        fixes.append("Increase the timeout threshold or split the task into smaller subtasks")

    # Import / module errors
    if "importerror" in root_lower or "modulenot" in root_lower or "import" in root_lower:
        fixes.append("Check that the dependency packages are installed (pip install -r requirements.txt)")

    # Permission / auth failures
    if any(k in root_lower for k in ("permission", "unauthorized", "forbidden", "403", "401")):
        fixes.append("Check that the authentication credentials and permission configuration are correct")

    # Network / connection failures
    if any(k in root_lower for k in ("connection", "network", "unreachable", "refused")):
        fixes.append("Check the network connection and whether the target service is available")

    # Generic fallback suggestions always appended
    if task.assigned_to:
        fixes.append(f"Contact the executing agent '{task.assigned_to}' to confirm the specific error details")
    if memos:
        issue_memos = [m for m in memos if m.get("type") == "issue"]
        if issue_memos:
            fixes.append(
                f"Refer to the {len(issue_memos)} issue records in the task memos "
                f"to troubleshoot the root cause"
            )

    if not fixes:
        fixes.append("View the task execution logs and confirm the environment prerequisites")
        fixes.append("Try reducing the task complexity and then re-execute")

    return fixes[:5]  # Return at most 5 suggestions


def _build_rollback_recommendation(task: Any) -> str:
    """Build a rollback recommendation based on task state."""
    from aiteam.types import TaskStatus

    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)

    if status_val == TaskStatus.FAILED.value if hasattr(TaskStatus, "FAILED") else "failed":
        return "Reset the task status to pending, fix the root cause, then reassign for execution"

    if status_val == TaskStatus.BLOCKED.value if hasattr(TaskStatus, "BLOCKED") else "blocked":
        return "After the dependency blockage is resolved, the task will automatically unlock and resume execution"

    # Running but suspected failed
    return "You can update the task status to pending and clear the result field, then re-execute"
