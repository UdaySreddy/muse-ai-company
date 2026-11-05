"""Compact projection views of list-type tool return bodies (the presentation layer; API routes and the data layer are
untouched).

See docs/tool-loading-design.md §4 for the design spec. Key points (backed by measurements from benchmark task
4267426d):

- By default ``fields="compact"`` returns a compact projection: task-wall saves 84.3% / events save
  50.7% / ecosystem lists save 80.7% of tokens; ``fields="all"`` is the escape hatch returning everything.
- The return body carries ``view`` + ``hint`` self-identification — so the agent clearly knows this is a compact view
  rather than missing fields, and points to the single-entity detail tools (user ruling 2026-07-14).
- The projection's three iron rules: keys needed by subsequent calls (id) are always complete; semantic content is
only degraded to truncated summaries,
  never deleted; signal fields the selection action needs (score/assigned_to/depends_on, etc.) are kept.

Roster-type tools (agent_list / team_status / team_briefing) have one more orthogonal rule (established after the
2026-08-03
member-roster blow-up incident): ``fields`` governs only **how wide the rows are**, and ``include_offline`` governs only
**which rows are included** — ``fields="all"`` does not mean "no filtering". The reason: in that measured 51-person
session team,
offline rows made up 96.4% of the payload; if ``fields="all"`` also brought all the offline rows back, the escape hatch
would itself exceed the cap, which equals having no escape hatch. Only both switches together reproduce today's
original return.
"""

from __future__ import annotations

import json
from typing import Any

# ------------------------------------------------------------------
# fields parameter parsing
# ------------------------------------------------------------------

_COMPACT_VALUES = ("", "compact")
_FULL_VALUES = ("all", "full")

FIELDS_ERROR = 'fields supports only "compact" (the default compact view) or "all" (all fields)'


def resolve_view(fields: str) -> str | None:
    """Normalize the fields parameter to "compact" / "all"; return None when unrecognizable."""
    value = (fields or "").strip().lower()
    if value in _COMPACT_VALUES:
        return "compact"
    if value in _FULL_VALUES:
        return "all"
    return None


# ------------------------------------------------------------------
# Generic truncation
# ------------------------------------------------------------------


def excerpt(text: str | None, max_chars: int) -> str:
    """Truncate long text into a summary (a semantics-preserving, non-deleting degradation)."""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


# ------------------------------------------------------------------
# Row-level projections (the whitelist is designed around the action "scan a list and choose")
# ------------------------------------------------------------------


def compact_task_row(task: dict[str, Any]) -> dict[str, Any]:
    """Task-wall row projection: all signals needed to pick a task are kept; descriptions/results are degraded to
    80-character summaries."""
    row: dict[str, Any] = {
        "id": task.get("id"),
        "title": task.get("title"),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "score": task.get("score"),
        "assigned_to": task.get("assigned_to"),
        "tags": task.get("tags") or [],
        "desc": excerpt(task.get("description"), 80),
    }
    # Sparse selection-related signals: included only when they have a value, keeping the default row size minimal
    if task.get("result"):
        row["result"] = excerpt(task.get("result"), 80)
    if task.get("depends_on"):
        row["depends_on"] = task["depends_on"]
    if task.get("subtasks"):
        row["subtask_count"] = len(task["subtasks"])
    return row


_EVENT_SUMMARY_KEYS = (
    "intent_summary",
    "tool_input_summary",
    "message",
    "summary",
    "title",
    "reason",
)


def compact_event_row(event: dict[str, Any]) -> dict[str, Any]:
    """Event row projection: the payload collapses into a one-line derived summary (not a deletion); always-empty
    fields are not output."""
    data = event.get("data") or {}
    summary = ""
    for key in _EVENT_SUMMARY_KEYS:
        value = data.get(key)
        if value:
            summary = str(value)
            break
    if not summary and data:
        summary = json.dumps(data, ensure_ascii=False)
    return {
        "id": event.get("id"),
        "type": event.get("type"),
        "source": event.get("source"),
        "ts": event.get("timestamp"),
        "summary": excerpt(summary, 60),
    }


def compact_reuse_candidate_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """Agent reuse candidate row projection: keep the decision signals + call keys,
    drop the verbose rationale/watermark detail (available via fields="all")."""
    return {
        # Call keys always kept in full: agent_id / cc id for SendMessage, session
        # id for claude --resume.
        "agent_id": candidate.get("agent_id"),
        "cc_tool_use_id": candidate.get("cc_tool_use_id"),
        "session_id": candidate.get("session_id"),
        # Selection signals for the three-way decision.
        "name": candidate.get("name"),
        "role": candidate.get("role"),
        "domain_match": candidate.get("domain_match"),
        "ctx_pct": candidate.get("ctx_pct"),
        "ctx_tokens": candidate.get("ctx_tokens"),
        "availability": candidate.get("availability"),
        "recommended_action": candidate.get("recommended_action"),
        # Actionable next step (holds the addressing id; kept whole, not excerpted).
        "resume_hint": candidate.get("resume_hint"),
    }


def compact_agent_row(agent: dict[str, Any]) -> dict[str, Any]:
    """Team member row projection: call keys + the who-is-doing-what signals.

    Dropped here (all reachable via fields="all"): system_prompt - 24.7% of the
    measured payload on its own - plus config, the ctx_* watermark, the token
    ledger, trust_score and the transcript pointer. Kept whole because follow-up
    calls need them verbatim: ``id`` (agent_update_status) and ``name``
    (SendMessage addresses teammates by name).
    """
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "role": agent.get("role"),
        "status": agent.get("status"),
        "current_task": excerpt(agent.get("current_task"), 80),
        "last_active_at": agent.get("last_active_at"),
    }


def minimal_agent_row(agent: dict[str, Any]) -> dict[str, Any]:
    """Offline member digest row: identity plus when it went quiet, nothing else.

    An offline row is a terminated process: it cannot be messaged and cannot be
    assigned work, so the only questions it still answers are "who was here" and
    "when". current_task is deliberately absent - on an offline row it is stale
    by definition and reads as if the agent were still working.
    """
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "role": agent.get("role"),
        "last_active_at": agent.get("last_active_at"),
    }


def compact_template_row(template: dict[str, Any]) -> dict[str, Any]:
    """Agent template row projection: what you need to pick a subagent_type.

    body_preview and the per-template disallowedTools array are the weight here;
    the tool surface that matters when choosing a template is name + what it is for.
    """
    return {
        "name": template.get("name"),
        "desc": excerpt(template.get("description"), 120),
        "model": template.get("model"),
        "source": template.get("source"),
        "restricted": bool(template.get("disallowedTools")),
    }


def compact_wf_agent_row(row: dict[str, Any]) -> dict[str, Any]:
    """Workflow agent telemetry row projection.

    prompt_preview + result_preview were 53% of a measured 268,753-char
    workflow_get response. Identity, phase, cost and state stay; the two preview
    blobs degrade to excerpts; the four near-identical timestamps and the
    constant-per-run keys (run_id / wf_id / project_id) drop out entirely.
    """
    row_out: dict[str, Any] = {
        "label": row.get("label"),
        "state": row.get("state"),
        "model": row.get("model"),
        "phase": row.get("phase_index"),
        "tokens": row.get("tokens"),
        "tool_calls": row.get("tool_calls"),
        "duration_ms": row.get("duration_ms"),
        # Drill-down keys kept whole: os_agent_id joins the OS ledger.
        "os_agent_id": row.get("os_agent_id"),
        "result": excerpt(row.get("result_preview"), 100),
    }
    if row.get("last_tool_name"):
        row_out["last_tool"] = row.get("last_tool_name")
    return row_out


def compact_team_row(team: dict[str, Any]) -> dict[str, Any]:
    """Team list row projection: the keys needed to pick a team and drill in."""
    row: dict[str, Any] = {
        "id": team.get("id"),
        "name": team.get("name"),
        "status": team.get("status"),
        "kind": (team.get("config") or {}).get("kind", ""),
        "project_id": team.get("project_id"),
        "created_at": team.get("created_at"),
    }
    if team.get("summary"):
        row["summary"] = excerpt(team.get("summary"), 80)
    return row


def compact_profile_row(profile: dict[str, Any]) -> dict[str, Any]:
    """Ecosystem library profile row projection: the 5 fields needed to scan a list and fix a drill-down target."""
    summary = (
        profile.get("one_line_summary")
        or profile.get("description_excerpt")
        or profile.get("description")
    )
    return {
        "repo": profile.get("repo_full_name"),
        "stars": profile.get("stars"),
        "lang": profile.get("language") or "",
        "status": profile.get("stage_status"),
        "summary": excerpt(summary, 120),
    }


# Even excerpted, an activity row is bounded at roughly 420 chars, so this cap
# keeps the widest legal window near 17k - a window wider than this walks back
# toward the ceiling the projection just pulled us away from.
ACTIVITY_COMPACT_CAP = 40

# Same rule for the event firehose: compact rows measured ~230 chars each, so 60
# keeps the widest window near 14k. limit>=85 measured 23,182 chars - past the
# ceiling again. fields="all" stays uncapped: pulling the full archive is an
# explicit choice, not a wide default.
EVENT_COMPACT_CAP = 60


def compact_activity_row(activity: dict[str, Any]) -> dict[str, Any]:
    """Activity row projection: the untruncated tool I/O blobs become excerpts.

    output_summary routinely carries a whole command transcript (500+ chars a
    row measured), which is what pushed a 60-row window to 43.9k chars. The row
    id is dropped on purpose - no tool takes an activity id, so it is 42 chars a
    row of pure weight; agent_id stays because it is the key you drill down on.
    """
    row: dict[str, Any] = {
        "agent_id": activity.get("agent_id"),
        "tool": activity.get("tool_name"),
        "status": activity.get("status"),
        "ts": activity.get("timestamp"),
        "duration_ms": activity.get("duration_ms"),
        "input": excerpt(activity.get("input_summary"), 80),
        "output": excerpt(activity.get("output_summary"), 100),
    }
    if activity.get("error"):
        row["error"] = excerpt(activity.get("error"), 80)
    return row


# ------------------------------------------------------------------
# Roster projections (agent_list / team_status / team_briefing share the same semantics)
# ------------------------------------------------------------------

# The team roster endpoint caps at 200 rows; ask for the whole cap so the status
# counts below describe the real roster instead of one page of it.
ROSTER_FETCH_LIMIT = 200
# Offline rows folded into a count keep this many most-recent entries visible,
# so "who was on this team" never disappears without a trace.
OFFLINE_PREVIEW_DEFAULT = 5


def _last_seen(agent: dict[str, Any]) -> str:
    """Sort key for 'most recently alive first' (missing timestamps sort last)."""
    return str(agent.get("last_active_at") or agent.get("created_at") or "")


def project_roster(
    agents: list[Any],
    *,
    include_offline: bool = False,
    offline_preview: int = OFFLINE_PREVIEW_DEFAULT,
    view: str = "compact",
) -> dict[str, Any]:
    """Split a team roster into "who is here now" plus an offline digest.

    Measured on the real 51-member session container team (2026-08-03): offline
    rows were 96.4% of the agent_list payload and system_prompt alone 24.7%,
    which put both agent_list and team_status past the MCP result ceiling and
    made two of the most-used observability tools fail outright.

    Why offline is the part that folds: an offline agent is a terminated
    process. It cannot be messaged, cannot be assigned work, and its
    current_task is stale - it earns the least decision value per byte of any
    row in the payload. Nothing is deleted: the count is always reported, the
    most recent entries stay visible, and include_offline=True brings the whole
    roster back (agents rows are an audit ledger and are never dropped).

    Returns a dict with members (projected rows honouring `view`), counted,
    status_counts, and - unless include_offline - an offline digest block.
    """
    rows = [a for a in agents if isinstance(a, dict)]
    counts: dict[str, int] = {}
    for agent in rows:
        key = str(agent.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    offline = [a for a in rows if str(a.get("status") or "") == "offline"]
    listed = rows if include_offline else [a for a in rows if str(a.get("status") or "") != "offline"]
    project = (lambda a: a) if view == "all" else compact_agent_row
    out: dict[str, Any] = {
        "counted": len(rows),
        "status_counts": counts,
        "members": [project(a) for a in listed],
    }
    if not include_offline:
        preview = sorted(offline, key=_last_seen, reverse=True)[: max(0, offline_preview)]
        out["offline"] = {
            "count": len(offline),
            "recent": [minimal_agent_row(a) for a in preview],
        }
    return out


def page(rows: list[Any], limit: int, offset: int, cap: int = ROSTER_FETCH_LIMIT) -> tuple[list[Any], int, bool]:
    """Clamp+slice a projected row list. Returns (page, effective_limit, has_more)."""
    size = max(1, min(int(limit or 1), cap))
    start = max(0, int(offset or 0))
    end = start + size
    return rows[start:end], size, end < len(rows)


# ------------------------------------------------------------------
# Self-identifying hints (user ruling: the agent must know this is the compact version, not missing fields)
# ------------------------------------------------------------------

TASK_WALL_HINT = (
    "Compact view (not a missing field): for a single task in full use task_status(task_id),"
    'for historical progress use task_memo_read(task_id); this tool with fields="all" returns all fields'
)
EVENT_HINT = (
    "Compact view (not a missing field): the summary is derived from the event payload;"
    'for the full payload use fields="all"'
)
ECO_LIST_HINT = (
    "Compact view (not a missing field): for a single repo's full profile use ecosystem_repo_get(repo_full_name);"
    'this tool with fields="all" returns all fields'
)
REUSE_HINT = (
    "Compact view (not a missing field): the candidate decision signals"
    "(domain_match/ctx_pct/availability/recommended_action) and the call keys are kept;"
    'for the full rationale and watermark details use fields="all"'
)
# The roster escape hatch's unified wording: offline members are only folded into a count; the rows are still in the
# DB (the agents table forbids deletion).
_ROSTER_ESCAPE = (
    "Offline members have been folded into a count + summaries of the most recent few (the rows are still in the DB, "
    "just not expanded):"
    "for the full historical roster use include_offline=True,"
    "to find reusable historical members use agent_reuse_recommend(query=...)"
)
AGENT_LIST_HINT = (
    "Compact view (not a missing field): large fields such as system_prompt/config/context watermark/usage are hidden,"
    "while the call keys id (for status changes) and name (for SendMessage addressing) are kept in full;"
    f'{_ROSTER_ESCAPE}; for all fields use fields="all"'
)
TEAM_STATUS_HINT = (
    "Compact view (not a missing field): members and active_tasks are both projection rows;"
    f"{_ROSTER_ESCAPE}; "
    'for a single task in full use task_status(task_id); for the whole team in full use fields="all"'
)
TEAM_LIST_HINT = (
    "Compact view (not a missing field): each team keeps only id/name/status/kind/project_id/created_at;"
    "for a single team's details use team_status(team_id); for all fields use fields=\"all\" (316 teams in full "
    "measured 148k characters, which exceeds the cap)"
)
TEAM_BRIEFING_HINT = (
    "Compact view (not a missing field): the member roster, recent events, and to-do tasks are all projection rows;"
    f"{_ROSTER_ESCAPE}; for all fields use fields=\"all\""
)
ACTIVITY_HINT = (
    "Compact view (not a missing field): input/output have been truncated into summaries (the original may be a "
    "whole command output);"
    'for the full records use fields="all" (60 records in full measured 44k characters, approaching the MCP cap)'
)
TEMPLATE_LIST_HINT = (
    "Compact view (not a missing field): each template keeps only name/desc/model/source/restricted,"
    "and grouped gives only the category-to-template-name index;"
    'for the full body preview and disallowedTools details use fields="all" (the full set measured 32k characters)'
)
WORKFLOW_GET_HINT = (
    "Compact view (not a missing field): run.result/summary and each agent's prompt/result "
    "previews have been truncated into summaries, and agent rows keep only identity/stage/cost/status and the "
    "os_agent_id drill-down key;"
    'for the full archive use fields="all" (the full profiles of 166 agents measured 269k characters, certain to '
    'exceed the MCP cap)'
)
