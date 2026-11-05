"""Wake actionable predicate — the single source of truth for "is there work in
flight the Leader should react to".

The criterion cornerstone of wake system v2 (docs/wake-loop-v2-design.md §7.1/§7.2). The event watcher polls it, and
the turn-end guard queries it at shutdown; both share the same criterion and never disagree on "whether any work is in
flight". The criterion is centralized in
Python (unit-testable); the bash watcher acts only as a dumb poller (zero SQL).

Design principles (aligned with session_probe.py): purely read-only and defensive — each sub-signal is wrapped in its
own try, and any failure
uniformly degrades to 0/empty, never raises, and never 500s. Better to under-report (the next /loop tick covers it) than
to let the watcher/guard
lose contact because of an endpoint anomaly.

Data key mapping (following the data's actual ownership, nothing invented):
- agents by team_id (the Leader session's team; sub-agents hang beneath it)
- workflow_runs by session_id (WorkflowRun.session_id = the Leader session that started it; a run carries its own
  workflow-<id> team ≠ the Leader team, so runs must never be filtered by team_id)
- task_memos / briefings by project_id (resolved from the team)

Time caliber: UTC is unified across the whole system (see aiteam/clock.py). The watermark returns
``utc_now().isoformat()``, a self-describing string carrying the ``+00:00`` offset; the watcher passes it back as-is,
forming a
monotonic watermark; the since side parses it back into aware-UTC and compares it directly against the stored values —
the two ends no longer rely on separate conventions; the string itself
carries the caliber with it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiteam.clock import ensure_utc, parse_utc, utc_now
from aiteam.types import AgentStatus

logger = logging.getLogger(__name__)

# The terminal-state set of workflow_runs (design §7.2): these are actionable (the last three in particular need
# remediation).
TERMINAL_RUN_STATUSES = frozenset({"completed", "interrupted", "killed", "failed"})
# Active states: still in flight and benign; they do not trigger a wake-up and serve only as the guard's "is any work in
# flight" measure.
LIVE_RUN_STATUSES = frozenset({"planned", "running"})

# Cap on the reasons list, to prevent response bloat
_MAX_REASONS = 12


def parse_since(raw: str | None) -> datetime | None:
    """Parse the since query parameter into an aware-UTC datetime; return None when it cannot be parsed (= no lower
    bound).

    Fault-tolerantly accepts ISO8601 with 'Z', with a timezone offset, or without an offset. Those without an offset are
    read as UTC,
    consistent with the watermark's emission caliber.
    """
    return parse_utc(raw)


def _after(ts: datetime | None, since: datetime | None) -> bool:
    """Whether ts is strictly later than since; since=None is treated as no lower bound (everything counts). A
    missing ts does not count."""
    if ts is None:
        return False
    if since is None:
        return True
    try:
        return ensure_utc(ts) > ensure_utc(since)
    except (TypeError, ValueError):
        return False


async def _resolve_project_id(repo: Any, team_id: str, project_id: str) -> str:
    """When project_id is omitted, resolve it from the team. On failure, return an empty string (the memo/briefing
    signals will degrade to 0)."""
    if project_id:
        return project_id
    if not team_id:
        return ""
    try:
        team = await repo.get_team(team_id)
        return (team.project_id or "") if team else ""
    except Exception:  # noqa: BLE001
        return ""


async def _agent_signals(
    repo: Any, team_id: str, session_id: str, since: datetime | None
) -> tuple[int, int, list[str]]:
    """Return (busy_agents, finished_agents_since, reasons).

    finished_agents_since is an approximation: AgentModel has no "status change timestamp", so it uses
    status ∈ {waiting, offline} with last_active_at > since to approximate "just finished work from busy".
    This is a secondary signal — a sub-agent finishing usually writes a task_memo, which new_memos_since strongly
    covers.
    """
    reasons: list[str] = []
    try:
        agents = []
        if team_id:
            agents = await repo.list_agents(team_id)
        elif session_id:
            agents = await repo.find_agents_by_session(session_id)
        busy = 0
        finished = 0
        for a in agents:
            status = getattr(a, "status", None)
            if status == AgentStatus.BUSY:
                busy += 1
            elif status in (AgentStatus.WAITING, AgentStatus.OFFLINE):
                if _after(getattr(a, "last_active_at", None), since):
                    finished += 1
                    if len(reasons) < _MAX_REASONS:
                        reasons.append(f"agent '{getattr(a, 'name', '?')}' finished work (awaiting relay)")
        return busy, finished, reasons
    except Exception:  # noqa: BLE001 — probe failure must not break the endpoint
        logger.warning("wake_actionable: agent signal failed", exc_info=True)
        return 0, 0, reasons


async def _run_signals(
    repo: Any, session_id: str, project_id: str, since: datetime | None
) -> tuple[int, int, list[str]]:
    """Return (live_runs, terminal_runs_since, reasons), filtered by session_id ownership."""
    reasons: list[str] = []
    try:
        runs = await repo.list_workflow_runs(project_id=project_id, limit=50)
        live = 0
        terminal = 0
        for r in runs:
            # Only count runs started by this Leader session (when session_id is empty, do not narrow by session)
            if session_id and getattr(r, "session_id", None) not in (session_id, None):
                continue
            status = getattr(r, "status", "")
            if status in LIVE_RUN_STATUSES:
                live += 1
            elif status in TERMINAL_RUN_STATUSES:
                if _after(getattr(r, "updated_at", None), since):
                    terminal += 1
                    if len(reasons) < _MAX_REASONS:
                        wf = getattr(r, "wf_id", "?")
                        reasons.append(f"run {wf} → {status}")
        return live, terminal, reasons
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: run signal failed", exc_info=True)
        return 0, 0, reasons


async def _memo_count(repo: Any, project_id: str, since: datetime | None) -> int:
    """The number of valid task_memos added after since (sub-agents reporting progress/summaries)."""
    if not project_id:
        return 0
    try:
        return int(await repo.count_valid_task_memos_since(project_id, since))
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: memo signal failed", exc_info=True)
        return 0


async def _pending_briefings(repo: Any, project_id: str) -> int:
    """The number of pending-decision briefings — a user-facing signal, display only; it does not trigger the
    Leader's own wake-up (design §7.2)."""
    try:
        briefings = await repo.list_briefings(status="pending", project_id=project_id)
        return len(briefings)
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: briefing signal failed", exc_info=True)
        return 0


async def compute_actionable(
    repo: Any,
    session_id: str = "",
    team_id: str = "",
    project_id: str = "",
    since_raw: str | None = None,
) -> dict:
    """Compute the wake-up criterion. Never raises: any internal failure degrades to a conservative value.

    actionable = finished_agents_since>0 OR terminal_runs_since>0 OR new_memos_since>0
    (briefings do not count toward triggering; busy_agents/live_runs let the guard judge "whether any work is in
    flight").
    """
    since = parse_since(since_raw)
    resolved_project = await _resolve_project_id(repo, team_id, project_id)

    busy_agents, finished_agents, agent_reasons = await _agent_signals(
        repo, team_id, session_id, since
    )
    live_runs, terminal_runs, run_reasons = await _run_signals(
        repo, session_id, resolved_project, since
    )
    new_memos = await _memo_count(repo, resolved_project, since)
    pending_briefings = await _pending_briefings(repo, resolved_project)

    reasons = agent_reasons + run_reasons
    if new_memos > 0:
        reasons.append(f"{new_memos} new task_memos (sub-agents reporting progress)")

    actionable = (finished_agents > 0) or (terminal_runs > 0) or (new_memos > 0)

    return {
        "actionable": actionable,
        "reasons": reasons[:_MAX_REASONS],
        "busy_agents": busy_agents,
        "live_runs": live_runs,
        "terminal_runs_since": terminal_runs,
        "finished_agents_since": finished_agents,
        "new_memos_since": new_memos,
        "pending_briefings": pending_briefings,
        "project_id": resolved_project,
        "watermark": utc_now().isoformat(),
    }
