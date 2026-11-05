#!/usr/bin/env python3
"""SubagentStart hook — inject OS environment context into sub-agents.

Usage: python -m aiteam.hooks.inject_subagent_context
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "subagent_sessions"
)


def _safe_session_id(session_id: str) -> str:
    """Strip anything that isn't alphanumeric, hyphen, or underscore to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)


def _mark_subagent_session(session_id: str) -> None:
    """Touch a marker file so workflow_reminder can skip Leader checks for this session."""
    if not session_id:
        return
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return
    try:
        os.makedirs(_SUBAGENT_MARKER_DIR, exist_ok=True)
        marker = os.path.join(_SUBAGENT_MARKER_DIR, safe_id)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


# Default API base URL — resolved dynamically from port file
_API_BASE = _get_api_url()
# Timeout for API calls (seconds) — keep short to avoid blocking agent startup
_API_TIMEOUT = 2


def _api_get(path: str):
    """Fetch JSON from the OS API. Returns parsed data or None on any failure."""
    try:
        url = f"{_API_BASE}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# Direction memories (memory system v2 P1): kind labels + injection truncation priority (constraint is kept first).
_MEM_KIND_LABEL = {
    "constraint": "Constraints / Guardrails",
    "design": "Design intent",
    "directive": "Working style",
    "preference": "Format preferences",
}

# Direction-memory injection fuse (memory v2.1, 2026-07-31): the direction layer's 3000-character total quota +
# formatting overhead.
# The semantics are **a fuse, not a budget** — the server write side already caps storage by per-bucket character
# quotas (1200+1500+300=3000),
# so under normal conditions truncation never triggers here. It covers exactly one anomaly: someone bypassing the API
# and editing
# the DB directly to blow up the direction layer — the injection should not drown the bookkeeping conventions and
# identity block that follow.
# The old value 900 was a "routine truncation line": the storage red line allowed 16,000 characters while injection
# gave only 900, and of 48 measured entries
# only the first 2-3 actually reached the agent; the rest were truncated into one line, "46 more entries".
# Hooks are pure-stdlib processes that do not import the aiteam package, so the constants are defined independently
# here (one copy in each of the two hooks,
# kept in sync with the byte-for-byte copies in plugin/hooks — the I1 machine check).
_MEM_INJECT_FUSE = 4300


def _render_identity(payload: dict) -> list:
    """"The "who you are" identity block — replacing the retired os-register self-registration ritual.

    The agent is automatically absorbed by the SubagentStart hook and should never have had to agent_register manually
    once more;
    the only thing it truly lacks is "my own row's agent_id" (needed for speaking in meetings and signing memos).
    Here, at the dispatch instant, one reverse lookup is done: send_event's SubagentStart fires in parallel with this
    hook,
    so the absorption may not be persisted yet and finding nothing is normal — at that point give a self-lookup
    fallback instruction instead of leaving it blank
    (layer A of the A+B double insurance; layer B is the server side's /api/agents/whoami session+name reverse lookup).
    """
    cc_agent_id = str(payload.get("agent_id") or "")
    agent_name = str(payload.get("agent_type") or "")
    session_id = str(payload.get("session_id") or "")
    lines = ["## Your OS Identity"]
    if agent_name:
        lines.append(f"- Name (SendMessage addresses by name using this name): {agent_name}")
    resolved = None
    try:
        import urllib.parse as _up
        qs = _up.urlencode(
            {"cc_agent_id": cc_agent_id, "session_id": session_id, "name": agent_name}
        )
        data = _api_get(f"/api/agents/whoami?{qs}")
        if isinstance(data, dict) and data.get("found"):
            resolved = data
    except Exception:
        resolved = None
    if resolved:
        lines.append(f"- agent_id: {resolved.get('agent_id')}")
        if resolved.get("team_id"):
            lines.append(f"- team_id: {resolved.get('team_id')}")
    else:
        lines.append(
            "- agent_id: not yet persisted (absorption runs parallel to this injection). When needed, look it up "
            "yourself via "
            "GET /api/agents/whoami?name=<your name>&session_id=<session id>; "
            "there is no agent_register tool — absorption is automatic, no self-registration needed."
        )
    lines.append("")
    return lines


def _project_dir() -> str:
    """The current project directory: CLAUDE_PROJECT_DIR first, falling back to cwd (for X-Project-Dir project
    resolution)."""
    return os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()


def _fetch_direction_memories() -> list:
    """Fetch valid direction-layer entries (the API has ordered them by kind priority). Returns [] when unreachable
    (silent).

    Passes the X-Project-Dir header so the API resolves the current project, including project-level direction entries +
    global/user global entries — this is "every dispatched agent inherits the direction layer at birth".
    """
    try:
        import urllib.parse as _up
        req = urllib.request.Request(f"{_API_BASE}/api/memories", method="GET")
        pdir = _project_dir()
        if pdir:
            req.add_header("X-Project-Dir", _up.quote(pdir, safe="/:.-_\\"))
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _sanitize_inline(text: str) -> str:
    """Single-line sanitization before injection rendering (review major: memo/memory content containing newlines can
    forge
    a '## section header' and pollute other agents' injected context). Collapses all whitespace into single spaces."""
    return " ".join((text or "").split())


def _render_direction_memories(items: list, budget: int = _MEM_INJECT_FUSE) -> list:
    """Render direction-layer entries; only when the fuse is exceeded, truncate by kind priority and note the
    remaining count (never triggers normally)."""
    if not items:
        return []
    lines = ["## Directional memories (team-shared · you must comply)"]
    used = 0
    truncated = 0
    stop = False
    for m in items:
        if stop:
            truncated += 1
            continue
        content = _sanitize_inline(m.get("content") or "")
        if not content:
            continue
        label = _MEM_KIND_LABEL.get(m.get("kind", "preference"), m.get("kind", ""))
        entry = f"- [{label}] {content}"
        if used + len(entry) > budget:
            stop = True
            truncated += 1
            continue
        lines.append(entry)
        used += len(entry)
    if truncated:
        lines.append(f"- …{truncated} more entries; the Leader can view them with memory_list")
    lines.append("")
    return lines


def _fetch_recent_task_memos(task_id: str, limit: int = 3) -> list:
    """Fetch the current task's most recent `limit` valid memos (the "recent records" of Zep's dual read). Returns
    [] when unreachable."""
    if not task_id:
        return []
    try:
        req = urllib.request.Request(
            f"{_API_BASE}/api/tasks/{task_id}/memo", method="GET"
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        memos = data.get("data", []) if isinstance(data, dict) else []
        recent = memos[-limit:]
        rendered = ["## Recent records of the current task (episodic layer)"]
        for m in recent:
            content = _sanitize_inline(m.get("content") or "")
            if content:
                rendered.append(f"- [{m.get('type', 'progress')}] {content[:150]}")
        rendered.append("")
        return rendered if len(rendered) > 2 else []
    except Exception:
        return []


# P0 reconnection (audit, 2026-07-14): the memo injection's trigger key is taken directly from this dispatch's prompt.
# The old implementation hung on the retired config.pipeline detection — constantly empty (the injection never took
# effect), and every dispatch
# empty-scanned all teams (1+N API calls), while dead copy still taught agents to call the retired
# pipeline-advancement tools.
# Historical execution-pattern injection was retired in batch 8a on 2026-07-27 together with
# pattern_record/pattern_search
# (storage constantly empty; the injection was forever an empty section).
_TASK_ID_RE = re.compile(
    r"(?:task_id|(?i:task\s+id)|(?i:task\s+wall)|(?i:overall\s+task)|\u4efb\u52a1\s*ID|\u4efb\u52a1\u5899|"
    r"\u603b\u4efb\u52a1)[^0-9a-fA-F]{0,12}"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _extract_task_context(payload: dict) -> tuple[str, str]:
    """Extract (task_id, prompt text) from the dispatch context.

    The prompt recognizes only the dispatch fields the SubagentStart payload itself carries: payload.prompt /
    payload.description; when both are empty, fall back to the agent_type + cwd combination (which cannot hit a
    task_id, nor pollute the injection).

    **Never read the transcript**: the payload's transcript_path points at the parent session's (the Leader's)
    transcript, whose first user message is "what the user said to the Leader", not this dispatch's prompt.
    The old fallback worked from it, making task_id extraction and memo fetching run on the wrong text
    (batch 3 wrong-object fix, 2026-07-27).

    task_id recognizes only explicit styles (task_id=<uuid>, Task ID: <uuid>, etc.; see _TASK_ID_RE),
    avoiding uuids like repo_id/deep_review_id being mistaken for tasks.
    """
    prompt = str(payload.get("prompt") or payload.get("description") or "")
    if not prompt.strip():
        parts = [
            str(payload.get("agent_type") or ""),
            str(payload.get("cwd") or ""),
        ]
        prompt = " ".join(p for p in parts if p)
    prompt = prompt[:2000]
    match = _TASK_ID_RE.search(prompt)
    return (match.group(1) if match else "", prompt)


def main():
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    _mark_subagent_session(payload.get("session_id", ""))

    # Build injection content
    lines = []
    # Keep only "what you cannot derive": the OS bookkeeping conventions + the project's writing conventions.
    # The safety-rules section was deleted (2026-07-28): the three rules rm -rf/secrets/git add .env are both common
    # sense and already
    # hard-blocked by workflow_reminder's S1/S2/S3 before the command lands; writing them into the injection would be
    # pure duplication.
    # The report-format section was deleted (audit P2, 2026-07-14): it duplicated the direction memory's "report as
    # soon as done" directive,
    # and it misled one-shot question-answering agents (it once made a pure Q&A agent attach the full report
    # boilerplate).
    lines.append("=== AI Team OS Sub-Agent Environment ===")
    lines.append("")
    lines.append("## Bookkeeping conventions (OS-specific)")
    lines.append("- When taking a task, first read the historical progress with task_memo_read; on completion, "
                 "write a summary with task_memo_add(type=summary)")
    lines.append(
        "- After every 2 substantive operations (editing files/running commands/creating resources), record progress "
        "with task_memo_add — "
        "the context can be compacted at any time; persisting to disk is the only line of defense"
    )
    lines.append(
        "- Persist research output with report_save; do not Write it into .md files (writing directly does not enter "
        "the DB and is not tracked)"
    )
    lines.append("- If the same approach fails 3 times in a row, switch approach or escalate to the Leader; do not "
                 "keep retrying")
    lines.append("")
    lines.append("## Writing conventions")
    lines.append("- Code comments and git commit messages in English; documents in the project language (this "
                 "project: Chinese)")
    lines.append("")

    # Identity block (the replacement after os-register's retirement): silently skipped — an unreachable API must
    # never make the hook error.
    try:
        lines.extend(_render_identity(payload))
    except Exception:
        pass

    # Direction-memory section (memory system v2 P1): every dispatched agent inherits the team's direction layer at
    # birth.
    # Silently skipped — an unreachable API must never make the hook error.
    try:
        lines.extend(
            _render_direction_memories(
                _fetch_direction_memories(), budget=_MEM_INJECT_FUSE
            )
        )
    except Exception:
        pass

    # Dynamic injection trigger key: directly from this dispatch's prompt (P0 reconnection; no longer depends on the
    # retired pipeline)
    task_id_for_memos = ""
    try:
        task_id_for_memos, _prompt_text = _extract_task_context(payload)
    except Exception:
        pass

    # The current task's 3 most recent valid memos (the "recent records" of Zep's dual read; silently skipped)
    try:
        lines.extend(_fetch_recent_task_memos(task_id_for_memos, limit=3))
    except Exception:
        pass

    # Try to read current team info — inject only **this session's** team.
    #
    # The old implementation iterated all directories under ~/.claude/teams and injected every one as "the current
    # team": since CC v2.1.219
    # every session builds its own session-<id> directory, so a subagent would receive several sessions' team blocks
    # at once
    # (live proof 2026-07-27: one injection had 4 "current teams" side by side, 3 of them belonging to other
    # sessions). A crossed-over member
    # roster would mislead a subagent into SendMessage-ing someone who is not in its team at all.
    # The criterion uses leadSessionId == this event's session_id — that is the authoritative ownership CC itself
    # writes into config.json,
    # more reliable than the directory name (an internal id, not from the same source as the hook's session_id).
    session_id = payload.get("session_id", "") if isinstance(payload, dict) else ""
    teams_dir = os.path.join(os.path.expanduser("~"), ".claude", "teams")
    if session_id and os.path.isdir(teams_dir):
        for team_dir in os.listdir(teams_dir):
            config_path = os.path.join(teams_dir, team_dir, "config.json")
            if os.path.isfile(config_path):
                try:
                    with open(config_path, encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("leadSessionId") != session_id:
                        continue  # another session's team; do not inject
                    members = data.get("members", [])
                    if members:
                        lines.append(f"## Current Team: {team_dir}")
                        lines.append(f"Members: {', '.join(m.get('name', '?') for m in members)}")
                        lines.append("")
                except Exception:
                    pass

    # Trim context to avoid overwhelming sub-agent with boilerplate.
    # Keep the mandatory header rules (first ~40 lines) and dynamic sections.
    # If total lines exceed the budget, drop the team-membership section (lowest priority).
    # The cap was raised to 100 along with the direction-layer quota (memory v2.1): once the direction layer allows
    # 3000 characters, the entry line count alone can
    # approach the old 60-line threshold, and trimming by the old value would routinely cut the team roster — the sole
    # basis for SendMessage's
    # by-name addressing. This is a knock-on item of the quota axis change, not a relaxation of the total injection
    # volume.
    _max_lines = 100
    if len(lines) > _max_lines:
        # Find where team membership blocks start (marked by "## Current Team:")
        team_block_start = next(
            (i for i, ln in enumerate(lines) if ln.startswith("## Current Team:")), None
        )
        if team_block_start is not None:
            lines = lines[:team_block_start]

    # Output
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
