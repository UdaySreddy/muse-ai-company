"""AI Team OS — System rules query routes.

Provides query interfaces for system automated rules and advisory rules,
replacing verbose rule descriptions in CLAUDE.md.

Also exposes a graceful self-shutdown endpoint used by the standardized
restart flow (os_restart_api MCP tool).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


# Category A: Code-enforced automated rules (no human intervention needed)
_AUTOMATED_RULES: list[dict] = [
    {
        "id": "A1",
        "category": "agent-lifecycle",
        "name": "Agent persistence entry point",
        "description": "Agents persisted via POST /api/agents are marked source=api and set to busy by default. There is no corresponding MCP tool — sub-agents need not register themselves; A2 enrolls them automatically",
        "enforced_by": "src/aiteam/api/routes/agents.py — add_agent",
    },
    {
        "id": "A2",
        "category": "agent-lifecycle",
        "name": "Hook automatic fallback",
        "description": "The SubagentStart event automatically updates a registered Agent's status to busy",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_start",
    },
    {
        "id": "A3",
        "category": "agent-lifecycle",
        "name": "SubagentStop→waiting",
        "description": "The SubagentStop event sets the Agent to waiting (waiting for input, not closed). Three states: busy (working) / waiting (waiting) / offline (closed)",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_stop",
    },
    {
        "id": "A4",
        "category": "agent-lifecycle",
        "name": "SessionEnd→offline",
        "description": "When a session ends, all agents are set to offline (closed) and their session_id is cleared",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_end",
    },
    {
        "id": "A5",
        "category": "agent-lifecycle",
        "name": "Stop→offline",
        "description": "When the CC process terminates, hook-source agents are set to offline",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_stop",
    },
    {
        "id": "A6",
        "category": "agent-lifecycle",
        "name": "Status self-healing",
        "description": "When a WAITING Agent receives a tool event, it is automatically corrected to BUSY",
        "enforced_by": "src/aiteam/api/hook_translator.py — _self_heal_agent",
    },
    {
        "id": "A8",
        "category": "session",
        "name": "Session-Leader reuse",
        "description": "At SessionStart, look up the existing Leader by project and reuse it, avoiding the creation of ghost agents",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_start",
    },
    {
        "id": "A9",
        "category": "session",
        "name": "Automatic project creation",
        "description": "At SessionStart, if no project matches, one is automatically created from the cwd",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_start",
    },
    {
        "id": "A10",
        "category": "conflict-detection",
        "name": "File edit conflict detection",
        "description": "When the same file is edited by multiple Agents, a file.edit_conflict event is emitted",
        "enforced_by": "src/aiteam/api/hook_translator.py — _check_file_edit_conflict",
    },
    {
        "id": "A11",
        "category": "conflict-detection",
        "name": "Hot file tracking",
        "description": "An in-memory tracker counts hot files edited by multiple Agents, for use by team_briefing",
        "enforced_by": "src/aiteam/api/hook_translator.py — _FileEditTracker",
    },
    {
        "id": "A12",
        "category": "activity-tracking",
        "name": "Tool usage recording",
        "description": "PreToolUse/PostToolUse events are automatically recorded to AgentActivity",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_pre_tool_use / _on_post_tool_use",
    },
    {
        "id": "A13",
        "category": "activity-tracking",
        "name": "current_task auto-extracted from role",
        "description": "When an Agent is persisted, if its role contains the ' — ' separator it is automatically split into role + current_task ('Frontend Engineer — Dashboard development' → role='Frontend Engineer', current_task='Dashboard development')",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_start + routes/agents.py — add_agent",
    },
    {
        "id": "A14",
        "category": "activity-tracking",
        "name": "last_active_at auto-update",
        "description": "Every tool call automatically updates the Agent's last active time",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_pre_tool_use / _on_post_tool_use",
    },
    {
        "id": "A15",
        "category": "event-system",
        "name": "Event bus broadcast",
        "description": "All status changes emit events through the EventBus for real-time WebSocket push",
        "enforced_by": "src/aiteam/api/event_bus.py + src/aiteam/api/routes/ws.py",
    },
    {
        "id": "A16",
        "category": "type-safety",
        "name": "Shared type definitions",
        "description": "All data models are defined centrally in types.py; modules only reference them read-only",
        "enforced_by": "src/aiteam/types.py",
    },
    {
        "id": "A17",
        "category": "task-management",
        "name": "Task dependency auto-blocking",
        "description": "Tasks with unfinished dependencies are automatically marked as blocked",
        "enforced_by": "TaskStatus.BLOCKED + depends_on field",
    },
    {
        "id": "A18",
        "category": "hooks",
        "name": "Unified hook script entry point",
        "description": "CC hook events are uniformly POSTed via send_event.py to /api/hooks/event (coverage is as defined in plugin/hooks/hooks.json)",
        "enforced_by": "plugin/hooks/send_event.py",
    },
]

# Category B: Rules requiring human judgment (with advice)
_ADVISORY_RULES: list[dict] = [
    {
        "id": "B-1",
        "category": "memory",
        "name": "Layered memory responsibilities",
        "description": "When the user gives a preference/correction/design intent, the Leader immediately memory_adds it to the direction layer; task process records go through task_memo_add; do not mix the two",
        "advice": "Write test: how many future tasks can this entry affect? If it affects only a single task, send it to task_memo. The direction layer's storage cap = injection budget (bucket character quotas: global 1200 / project 1500 / user 300, each entry ≤400 characters); if over the limit, first invalidate or merge within the same round to free space and then retry; when a preference changes, use supersedes replacement instead of physically deleting",
    },
    {
        "id": "B0.1",
        "category": "meeting",
        "name": "Meeting trigger conditions",
        "description": "Any proposal involving deliberate decision revision / cutting tools or dropping tables / changing machine-check red lines must go through a meeting first (Council discipline 4, user-approved). Judge other cases yourself — an empty task wall does not mean a meeting is needed; do not hold meetings just to have something to do",
        "advice": "A meeting closes with decisions put on the wall: conclusions are turned into decision records or task wall entries, otherwise the meeting counts as never held",
    },
    {
        "id": "B0.3",
        "category": "leadership",
        "name": "Member tool restrictions are resolved by the Leader",
        "description": "When a member reports a tool/permission restriction, the Leader installs or changes configuration; members must not bypass it themselves",
        "advice": "When an MCP tool is unavailable, use /mcp → Reconnect to refresh",
    },
    {
        "id": "B0.4",
        "category": "leadership",
        "name": "Do not pass team_name when dispatching",
        "description": "The Agent team_name parameter is deprecated and ignored; do not pass it. Every session carries its own unique implicit team, and dispatched agents are automatically enrolled by the SubagentStart hook into the session-<sid8> container team",
    },
    {
        "id": "B0.5",
        "category": "leadership",
        "name": "Claim task-wall tasks flexibly — not limited to short-term",
        "description": "You may claim mid/long-term tasks directly; you need not only do short-term ones. After decomposing, withdraw the parts not being implemented yet, to avoid zombie tasks",
    },
    {
        "id": "B0.6",
        "category": "leadership",
        "name": "Project memory maintenance — ensure recoverability",
        "description": "As soon as a milestone goal is completed, record it in memory (what was done / why the decision was made / current state / next steps) so context can be recovered after a compact or restart; keep the task wall, meeting records, and memory consistent",
    },
    {
        "id": "B0.7",
        "category": "leadership",
        "name": "Do not idle-wait — keep claiming tasks after dispatching",
        "description": "While waiting for member results, do not sit idle; go back to the task wall for work that can run in parallel; no more than 3 parallel short-term tasks",
    },
    {
        "id": "B0.8",
        "category": "leadership",
        "name": "Sync behavior changes to QA",
        "description": "When a change affects system behavior or frontend display, proactively tell QA what to observe; pure documentation/rule changes are not notified",
    },
    {
        "id": "B0.9",
        "category": "leadership",
        "name": "The Leader focuses on coordination, not concrete implementation",
        "description": "The Leader only does task assignment, decisions, and driving progress. Tasks requiring reading multiple files / writing code / debugging are always dispatched to members; only tiny changes like editing one line of config are done personally",
        "advice": "User ruling: a Leader sinking into concrete implementation stalls the whole project",
    },
    {
        "id": "B0.11",
        "category": "leadership",
        "name": "The Leader sets the agent's current task",
        "description": "After dispatching an agent, use agent_update_status to set current_task (role holds a short role name, current_task holds the concrete task) — the Dashboard shows what members are doing based on this",
    },
    {
        "id": "B0.12",
        "category": "leadership",
        "name": "Task memo tracking",
        "description": "Before claiming a task, task_memo_read the history; during execution, task_memo_add key progress and decisions; after completion, task_memo_add(type='summary') to write the summary",
    },
    {
        "id": "B2",
        "category": "agent-lifecycle",
        "name": "Resident members vs temporary members",
        "description": "QA and bug-fixer are resident members, kept for the project's duration; research / one-off implementation types are temporary members, reclaimed after delivery. The team is kept until the project completes",
    },
    {
        "id": "B3",
        "category": "memory",
        "name": "Memory authority hierarchy",
        "description": "On information conflicts: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem",
        "advice": "Only record human intent that cannot be derived; leave technical details to the code and git",
    },
    {
        "id": "B4",
        "category": "context",
        "name": "Context management - WARNING",
        "description": "On receiving [CONTEXT WARNING], finish the current minimal atomic task and then save progress",
        "advice": "Record what is completed / in progress / the next-step plan, and remind the user to /compact",
    },
    {
        "id": "B5",
        "category": "context",
        "name": "Context management - CRITICAL",
        "description": "On receiving [CONTEXT CRITICAL], stop immediately, start no new tasks, urgently persist all progress, and remind the user to /compact",
    },
    {
        "id": "B0.14",
        "category": "leadership",
        "name": "Action items must go on the wall",
        "description": "Action items arising in conversations/meetings must be put on the wall via task_create — verbal promises do not count",
    },
    {
        "id": "B0.16",
        "category": "leadership",
        "name": "Leader autonomous operation mode",
        "description": "Advance autonomously by task-wall priority without waiting for user confirmation at each step. Make tactical decisions autonomously; only escalate strategic decisions such as project direction / major architecture to the user; when blocked, switch to another task and keep advancing",
        "advice": "When the user speaks, report progress uniformly + list the items awaiting decision",
    },
    {
        "id": "B0.16b",
        "category": "leadership",
        "name": "Pending decisions must be queued, not left only in reports",
        "description": (
            "For anything in a sub-agent's completion report marked 'left for the user/Leader to decide', the dispatcher (Leader)"
            "must immediately briefing_add it into the pending-decision queue. A decision item written in a report body equals not submitted —"
            "the user reads the briefing queue and will not dig through every completion report"
        ),
        "advice": "When harvesting reports, scan item by item for 'awaiting decision / awaiting confirmation / recommend the user decide'; one briefing_add per item (with tags for filtering by topic)",
    },
    {
        "id": "B0.17",
        "category": "leadership",
        "name": "Research before implementing",
        "description": "For system-level new feature design, do external research + competitive analysis before acting; do not build in isolation by only looking at internal code",
    },
    {
        "id": "B6",
        "category": "meeting",
        "name": "Meeting discussion rules",
        "description": "Round 1 states viewpoints, Round 2+ cites and responds to previous speakers, and the final round summarizes",
        "advice": "Read previous speakers' messages before speaking, to avoid duplication or disconnects",
    },
    {
        "id": "B6.2",
        "category": "meeting",
        "name": "Meeting participant notification",
        "description": "meeting_create does not automatically notify anyone — you must SendMessage each participant the meeting_id and the topic, otherwise nobody will show up to the meeting",
    },
    {
        "id": "B0.18",
        "category": "execution",
        "name": "2-Action persistence rule",
        "description": "After every 2 substantive operations (editing files / running commands / creating resources), record progress with task_memo_add — the context can be compacted at any time, and persisting to disk is the only line of defense",
    },
    {
        "id": "B0.19",
        "category": "execution",
        "name": "3-failures escalation protocol",
        "description": "If the same method on the same task fails 3 times in a row, change course: change the method, ask another Agent for help, or escalate to the Leader; escalation triggers failure_analysis deposition",
    },
]


@router.get("/rules")
async def list_system_rules() -> dict:
    """List all system automated rules and advisory rules.

    - automated_rules (Category A): Code-enforced rules, no human intervention needed
    - advisory_rules (Category B): Rules requiring human judgment, with advice
    """
    return {
        "automated_rules": _AUTOMATED_RULES,
        "advisory_rules": _ADVISORY_RULES,
        "summary": {
            "automated_count": len(_AUTOMATED_RULES),
            "advisory_count": len(_ADVISORY_RULES),
            "categories": sorted({r["category"] for r in _AUTOMATED_RULES + _ADVISORY_RULES}),
        },
    }


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str) -> dict:
    """Query single rule details."""
    rule_id_upper = rule_id.upper()
    for rule in _AUTOMATED_RULES + _ADVISORY_RULES:
        if rule["id"] == rule_id_upper:
            rule_type = "automated" if rule_id_upper.startswith("A") else "advisory"
            return {"rule": rule, "type": rule_type}
    return {"error": f"Rule {rule_id} not found"}


def _wal_checkpoint_best_effort() -> None:
    """Run a SQLite WAL checkpoint on the default DB before exit.

    Best-effort only: any failure is logged and swallowed so it never blocks
    the shutdown. Uses stdlib sqlite3 against the file path (synchronous) rather
    than the async engine, since the event loop is being torn down at exit.
    """
    import sqlite3

    from aiteam.storage.connection import DEFAULT_DB_URL

    try:
        # DEFAULT_DB_URL looks like "sqlite+aiosqlite:///<path>"
        if "///" not in DEFAULT_DB_URL:
            return
        db_path = DEFAULT_DB_URL.split("///", 1)[-1]
        if not db_path:
            return
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — checkpoint must never block exit
        logger.warning("WAL checkpoint before shutdown failed (ignored): %s", exc)


async def _delayed_exit() -> None:
    """Wait briefly so the HTTP response is flushed, then hard-exit the process.

    os._exit is used (not sys.exit) to guarantee the uvicorn worker actually
    terminates and releases the port — sys.exit would only raise SystemExit
    inside the request task and could be swallowed by the server.
    """
    await asyncio.sleep(0.5)
    # Release the governance lease (added after the first exam failure on 2026-07-27): this endpoint hard-exits with os._exit, so the lifespan
    # teardown (the release in StateReaper.stop) never runs — the release must live on this real exit path,
    # otherwise a new instance is blocked by the dead pid's lease for the full TTL (180s) and governance silently stalls for three minutes. Best-effort; never block the exit.
    try:
        from aiteam.api import deps as _deps

        repo = getattr(_deps, "_repository", None)
        if repo is not None:
            released = await repo.release_governance_lease(f"api-{os.getpid()}")
            if released:
                logger.info("Governance lease released on shutdown (pid=%d)", os.getpid())
    except Exception:  # noqa: BLE001 — the exit path must never be blocked by this
        logger.debug("Lease release on shutdown failed (TTL will expire it)")
    _wal_checkpoint_best_effort()
    os._exit(0)


@router.post("/shutdown")
async def shutdown() -> dict:
    """Gracefully shut down this API process.

    Localhost-only by design: the API binds 127.0.0.1, so no external client can
    reach this endpoint and no extra auth is required. It exists to give the
    standardized restart flow (os_restart_api) a clean way to stop the old
    process before a new version is spawned on the same port.

    Returns immediately with the current PID, then self-exits ~0.5s later after a
    best-effort WAL checkpoint.
    """
    pid = os.getpid()
    logger.info("Graceful shutdown requested (pid=%d)", pid)
    asyncio.create_task(_delayed_exit())
    return {"success": True, "message": "shutting down", "pid": pid}
