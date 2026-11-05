"""AI Team OS — Watchdog checker + background patrol service.

Rule-driven quality gate that checks agent health, task health, and system health.
WatchdogChecker: Triggered on-demand by API endpoints, returns a list of alerts.
WatchdogRunner: Background asyncio.Task that periodically runs checks on all active teams.

Multi-DB support: WatchdogRunner scans all per-project databases in addition to the
default database each patrol cycle, with per-project error isolation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from aiteam.clock import utc_now
from aiteam.config.settings import WATCHDOG_CHECK_INTERVAL
from aiteam.loop.failure_alchemy import FailureAlchemist
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, TaskStatus, TeamStatus

if TYPE_CHECKING:
    from aiteam.api.event_bus import EventBus

logger = logging.getLogger(__name__)

# Threshold constants
AGENT_BUSY_TIMEOUT_MINUTES = 30
TASK_PENDING_TIMEOUT_MINUTES = 30

class WatchdogChecker:
    """Watchdog checker — rule-driven quality gate."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def run_all_checks(self, team_id: str) -> list[dict[str, Any]]:
        """Run all checks and return a list of alerts."""
        alerts: list[dict[str, Any]] = []

        alerts.extend(await self.check_agent_health(team_id))
        alerts.extend(await self.check_task_health(team_id))
        alerts.extend(await self.check_system_health())

        return alerts

    async def auto_recover_stuck_agents(self, team_id: str) -> list[dict]:
        """Detect and automatically recover stuck agents and their tasks."""
        recovered: list[dict] = []
        now = utc_now()
        agents = await self._repo.list_agents(team_id)
        all_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.RUNNING)

        # Build agent_id -> running tasks index
        running_tasks_by_agent: dict[str, list] = {}
        for task in all_tasks:
            if task.assigned_to:
                running_tasks_by_agent.setdefault(task.assigned_to, []).append(task)

        for agent in agents:
            if agent.status != AgentStatus.BUSY:
                continue

            ref_time = agent.last_active_at or agent.created_at
            elapsed_minutes = (now - ref_time).total_seconds() / 60

            if elapsed_minutes <= AGENT_BUSY_TIMEOUT_MINUTES:
                continue

            # Reset agent: WAITING + clear current_task
            await self._repo.update_agent(
                agent.id,
                status=AgentStatus.WAITING.value,
                current_task=None,
            )

            # Reset all running tasks for this agent to pending
            reset_tasks = []
            for task in running_tasks_by_agent.get(agent.id, []):
                await self._repo.update_task(
                    task.id,
                    status=TaskStatus.PENDING.value,
                    assigned_to=None,
                )
                reset_tasks.append(task.id)

                # Record recovery event to memory (agent scope)
                memo_content = (
                    f"Because agent '{agent.name}' was stuck (no activity for {elapsed_minutes:.0f} minutes), "
                    f"it was automatically reset and the task returned from RUNNING to PENDING."
                )
                await self._repo.create_memory(
                    scope="agent",
                    scope_id=agent.id,
                    content=memo_content,
                    metadata={
                        "type": "auto_recover",
                        "task_id": task.id,
                        "elapsed_minutes": round(elapsed_minutes, 1),
                    },
                )

            record = {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "reset_tasks": reset_tasks,
            }
            recovered.append(record)
            logger.warning(
                "Watchdog auto-recovery: Agent '%s' stuck for %.0f minutes, reset %d tasks",
                agent.name,
                elapsed_minutes,
                len(reset_tasks),
            )

        return recovered

    async def recover_failed_tasks(
        self,
        team_id: str,
        event_bus: EventBus | None = None,
    ) -> list[dict[str, Any]]:
        """Selector-pattern failed task recovery.

        retry_count < 2 -> reset to pending for retry
        retry_count >= 2 -> keep as failed, emit alert event
        retry_count is stored in task.config["retry_count"].
        """
        results: list[dict[str, Any]] = []
        failed_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.FAILED)

        for task in failed_tasks:
            retry_count: int = int(task.config.get("retry_count", 0))
            title = task.title or task.description[:60]

            if retry_count < 2:
                # Retry: reset to pending, increment retry_count
                new_config = {**task.config, "retry_count": retry_count + 1}
                await self._repo.update_task(
                    task.id,
                    status=TaskStatus.PENDING.value,
                    assigned_to=None,
                    config=new_config,
                )
                record: dict[str, Any] = {
                    "task_id": task.id,
                    "title": title,
                    "action": "retried",
                    "retry_count": retry_count + 1,
                }
                results.append(record)
                logger.info(
                    "Failed task retry: '%s' (retry=%d)",
                    title,
                    retry_count + 1,
                )
            else:
                # Exceeded retry limit: trigger failure alchemy, extract learning artifacts
                alchemist = FailureAlchemist(self._repo)
                alchemy_result = await alchemist.process_failure(task.id, team_id)

                record = {
                    "task_id": task.id,
                    "title": title,
                    "action": "max_retries_exceeded",
                    "retry_count": retry_count,
                    "alchemy": alchemy_result,
                }
                results.append(record)
                if event_bus is not None:
                    await event_bus.emit(
                        "watchdog.task_failed_permanently",
                        f"task:{task.id}",
                        {
                            "task_id": task.id,
                            "title": title,
                            "team_id": team_id,
                            "retry_count": retry_count,
                            "trigger": "recover_failed_tasks",
                        },
                    )
                logger.warning(
                    "Failed task exceeded the retry limit: '%s' (retry=%d), Leader intervention required",
                    title,
                    retry_count,
                )

        return results

    async def check_agent_health(self, team_id: str) -> list[dict[str, Any]]:
        """Check agent health: BUSY timeout (>30min), frequent crashes."""
        alerts: list[dict[str, Any]] = []
        now = utc_now()
        agents = await self._repo.list_agents(team_id)

        for agent in agents:
            # Check BUSY timeout
            if agent.status == AgentStatus.BUSY:
                ref_time = agent.last_active_at or agent.created_at
                elapsed_minutes = (now - ref_time).total_seconds() / 60

                if elapsed_minutes > AGENT_BUSY_TIMEOUT_MINUTES:
                    alerts.append(
                        {
                            "severity": "warning",
                            "category": "agent",
                            "title": f"Agent BUSY timeout: {agent.name}",
                            "description": (
                                f"Agent '{agent.name}' has been in the BUSY state for "
                                f"{elapsed_minutes:.0f} minutes (threshold {AGENT_BUSY_TIMEOUT_MINUTES} minutes). "
                                f"Last activity: {ref_time.isoformat()}"
                            ),
                            "suggested_action": (
                                f"Check whether Agent '{agent.name}' is stuck; "
                                "consider resetting via StateReaper or manually setting it to IDLE"
                            ),
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                        }
                    )

        return alerts

    async def check_task_health(self, team_id: str) -> list[dict[str, Any]]:
        """Check task health: long-pending (>30min), BLOCKED but dependencies completed."""
        alerts: list[dict[str, Any]] = []
        now = utc_now()
        all_tasks = await self._repo.list_tasks(team_id)

        # Build task_id -> task index
        task_map = {t.id: t for t in all_tasks}

        for task in all_tasks:
            # Check long-pending tasks
            if task.status == TaskStatus.PENDING:
                elapsed_minutes = (now - task.created_at).total_seconds() / 60

                if elapsed_minutes > TASK_PENDING_TIMEOUT_MINUTES:
                    alerts.append(
                        {
                            "severity": "warning",
                            "category": "task",
                            "title": f"Task PENDING for a long time: {task.title}",
                            "description": (
                                f"Task '{task.title}' has waited {elapsed_minutes:.0f} minutes "
                                f"(threshold {TASK_PENDING_TIMEOUT_MINUTES} minutes), "
                                f"priority: {task.priority}"
                            ),
                            "suggested_action": ("Assign an Agent to execute this task, or lower its priority"),
                            "task_id": task.id,
                        }
                    )

            # Check BLOCKED tasks whose dependencies are all completed
            if task.status == TaskStatus.BLOCKED and task.depends_on:
                deps_all_done = True
                for dep_id in task.depends_on:
                    dep_task = task_map.get(dep_id)
                    if dep_task is None:
                        continue
                    if dep_task.status != TaskStatus.COMPLETED:
                        deps_all_done = False
                        break

                if deps_all_done:
                    alerts.append(
                        {
                            "severity": "warning",
                            "category": "task",
                            "title": f"Task can be unblocked: {task.title}",
                            "description": (
                                f"Task '{task.title}' is in the BLOCKED state, but all dependency tasks are complete"
                            ),
                            "suggested_action": ("Update this task's status from BLOCKED to PENDING"),
                            "task_id": task.id,
                        }
                    )

        return alerts

    async def check_system_health(self) -> list[dict[str, Any]]:
        """Check system health: database reachability."""
        alerts: list[dict[str, Any]] = []

        # Check database connection
        try:
            await self._repo.list_teams()
        except Exception as e:
            alerts.append(
                {
                    "severity": "critical",
                    "category": "system",
                    "title": "Database connection anomaly",
                    "description": f"Unable to query the database: {e}",
                    "suggested_action": "Check the database configuration and connection state",
                }
            )

        return alerts


class WatchdogRunner:
    """Background watchdog patrol service — asyncio.Task pattern.

    Periodically iterates over all active teams, runs all WatchdogChecker checks,
    and emits alerts to EventBus. Pattern modeled after StateReaper.
    """

    def __init__(
        self,
        checker: WatchdogChecker,
        event_bus: EventBus,
    ) -> None:
        self._checker = checker
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._running = False
        # D3 stage C: shares the f"api-{pid}" lease-holder identity with the same-process StateReaper,
        # so the two governance loops in the same process renew each other and never contend.
        self._lease_holder = f"api-{os.getpid()}"

    def start(self) -> None:
        """Start the background patrol loop."""
        if self._task is not None:
            logger.warning("WatchdogRunner is already running; skipping the duplicate start")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="watchdog-runner")
        logger.info("WatchdogRunner started, interval=%ds", WATCHDOG_CHECK_INTERVAL)

    async def stop(self) -> None:
        """Stop the background patrol loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("WatchdogRunner stopped")

    async def _run_loop(self) -> None:
        """Main patrol loop — executes once every WATCHDOG_CHECK_INTERVAL seconds."""
        while self._running:
            try:
                await asyncio.wait_for(self._run_cycle(), timeout=30.0)
            except TimeoutError:
                logger.warning("Watchdog inspection cycle timed out (30s); skipping this round")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watchdog inspection cycle error")

            try:
                await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Patrol cycle — processes the default DB only."""
        # D3 stage C: governance leader lease (audit M50) — when multiple API instances coexist, only the lease holder
        # inspects; the same holder in the same process means mutual renewal. Lease-layer failures fail open (lossless
        # for a single instance).
        try:
            is_leader = await self._checker._repo.try_acquire_governance_lease(
                self._lease_holder, ttl_seconds=WATCHDOG_CHECK_INTERVAL * 3
            )
        except Exception:
            is_leader = True
        if not is_leader:
            logger.debug("Governance lease held by another instance — skipping watchdog cycle")
            return
        try:
            await self._run_cycle_for_repo(self._checker._repo)
        except Exception:
            logger.exception("Watchdog cycle failed")

    async def _run_cycle_for_repo(self, repo: StorageRepository) -> None:
        """Single patrol cycle for a specific repository — iterate all active teams."""
        teams = await repo.list_teams()
        active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]
        alert_count = 0

        # Use a checker scoped to the given repo
        checker = WatchdogChecker(repo)

        for team in active_teams:
            # First execute stuck-agent auto-recovery
            recovered = await checker.auto_recover_stuck_agents(team.id)
            for record in recovered:
                await self._event_bus.emit(
                    "watchdog.agent_recovered",
                    f"team:{team.id}",
                    record,
                )

            # Failed task retry/alert
            failed_results = await checker.recover_failed_tasks(
                team.id,
                event_bus=self._event_bus,
            )
            for record in failed_results:
                if record.get("action") == "retried":
                    await self._event_bus.emit(
                        "watchdog.task_retried",
                        f"team:{team.id}",
                        record,
                    )

            alerts = await checker.run_all_checks(team.id)
            for alert in alerts:
                await self._event_bus.emit(
                    "watchdog.alert",
                    f"team:{team.id}",
                    alert,
                )
                alert_count += 1

        if alert_count > 0:
            logger.warning("Watchdog inspection found %d alerts", alert_count)
        else:
            logger.debug("Watchdog inspection complete, no alerts")
