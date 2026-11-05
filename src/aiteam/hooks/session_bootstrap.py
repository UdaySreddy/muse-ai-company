#!/usr/bin/env python3
"""AI Team OS — Session startup bootstrap script.

Executed when SessionStart hook fires:
1. Detect if OS API is reachable
2. If reachable, output Leader briefing (task wall Top3, team status, rule reminders)
3. If not reachable, prompt to start service

Stdout output is injected into Claude's system prompt to guide Leader behavior.
Usage: python -m aiteam.hooks.session_bootstrap
Uses only Python standard library.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "subagent_sessions"
)
_SUBAGENT_MARKER_TTL_SECONDS = 24 * 3600


def _cleanup_stale_subagent_markers() -> None:
    """Delete sub-agent session markers older than 24h (best-effort, silent)."""
    try:
        if not os.path.isdir(_SUBAGENT_MARKER_DIR):
            return
        cutoff = time.time() - _SUBAGENT_MARKER_TTL_SECONDS
        for name in os.listdir(_SUBAGENT_MARKER_DIR):
            path = os.path.join(_SUBAGENT_MARKER_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
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


API_URL = _get_api_url()
CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "plugin" / "config"

# Update check cooldown: only check once every 24 hours
_UPDATE_CHECK_COOLDOWN_SECS = 24 * 60 * 60
_UPDATE_CHECK_STATE_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "last_update_check.json"


def _api_get(path: str, timeout: float = 2.0):
    """GET request to API; return JSON or None."""
    try:
        req = urllib.request.Request(f"{API_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
# formatting overhead
# (overhead allowance recalibrated for English kind labels, which run ~20 characters longer per entry than the Chinese
# ones).
# The semantics are **a fuse, not a budget** — the server write side already caps storage by per-bucket character
# quotas (1200+1500+300=3000),
# so under normal conditions truncation never triggers here. It covers exactly one anomaly: someone bypassing the API
# and editing
# the DB directly to blow up the direction layer — the briefing should not be drowned by the memory section.
# The old value 900 was a "routine truncation line": the storage red line allowed 16,000 characters while injection
# gave only 900, and of 48 measured entries
# only the first 2-3 actually reached the session; the rest were truncated into one line, "46 more entries".
# Hooks are pure-stdlib processes that do not import the aiteam package, so the constants are defined independently
# here (one copy in each of the two hooks,
# kept in sync with the byte-for-byte copies in plugin/hooks — the I1 machine check).
_MEM_INJECT_FUSE = 4300


def _fetch_direction_memories(
    project_id: str = "", project_dir: str = "", timeout: float = 2.0
) -> list:
    """Fetch valid direction-layer entries (the API has ordered them by kind priority + time descending). Returns []
    when unreachable (silent).

    A registered project passes X-Project-Id to take its project bucket directly; an unregistered directory passes
    X-Project-Dir so the API
    derives a temporary bucket from the directory fingerprint (dir:<sha1>), so it inherits only global/user + this
    directory's bucket and does not leak in
    other projects' project memories (root fix for the 2026-07-21 cross-wiring incident). The fingerprint derivation's
    sole authority is on the API side;
    the hook only passes the cwd over — avoiding copying the formula into the hook copies and causing silent drift.
    """
    try:
        req = urllib.request.Request(f"{API_URL}/api/memories", method="GET")
        if project_id:
            req.add_header("X-Project-Id", project_id)
        elif project_dir:
            import urllib.parse as _up
            req.add_header("X-Project-Dir", _up.quote(project_dir, safe="/:.-_\\"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    header = "=== Directional memories (team-shared · inherited by all dispatched agents) ==="
    lines = [header]
    used = 0  # The header is fixed overhead and is not charged to the budget (mirrors inject_subagent_context)
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
        entry = f"  [{label}] {content}"
        if used + len(entry) > budget:
            stop = True
            truncated += 1
            continue
        lines.append(entry)
        used += len(entry)
    if truncated:
        lines.append(f"  …{truncated} more entries in memory_list (truncated by kind priority)")
    lines.append("")
    return lines


def _load_team_config() -> dict | None:
    """Load team default configuration; return None on failure."""
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _build_auto_team_instructions(config: dict) -> list[str]:
    """Generate auto team creation instruction text based on config."""
    if not config.get("auto_create_team"):
        return []

    enabled_members = [m for m in config.get("permanent_members", []) if m.get("enabled")]
    if not enabled_members:
        return []

    # CC v2.1.219 status quo (tool surface checked 2026-07-25): the TeamCreate tool no longer exists, and Agent's
    # team_name parameter is annotated "Deprecated; ignored. The session has a single implicit team."
    # — each session carries its own single implicit team (built and managed by CC at
    # ~/.claude/teams/session-<internal id>/),
    # and dispatching an agent joins the team. So no longer instruct to create a team / pass a team name — just
    # dispatch members; OS-side ownership is handled by
    # hook_translator's SubagentStart automatic absorption.
    lines = []
    lines.append("")
    lines.append("=== Resident Member Dispatch Guide ===")
    lines.append("Just dispatch members directly (the team_name parameter is deprecated; do not pass it):")
    for i, m in enumerate(enabled_members, start=1):
        role = m["role"]
        lines.append(
            f"{i}. Agent(name='{m['name']}', "
            f"subagent_type='{role}', prompt='On standby, waiting for the Leader to assign tasks')"
        )
    return lines


def _resolve_project_root() -> "Path | None":
    """Resolve the project root directory from install_path.txt or package location fallback."""
    install_info_file = Path.home() / ".claude" / "data" / "ai-team-os" / "install_path.txt"
    if install_info_file.exists():
        try:
            candidate = Path(install_info_file.read_text(encoding="utf-8").strip())
            if candidate.is_dir() and (candidate / ".git").exists():
                return candidate
        except Exception:
            pass

    # Fallback: infer from package location (src/aiteam/hooks/ -> src/aiteam/ -> src/ -> project_root/)
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / ".git").exists():
        return candidate

    return None


def _get_remote_commit(project_root: "Path") -> str:
    """Fetch from origin and return the short hash of the remote HEAD (main or master)."""
    subprocess.run(
        ["git", "fetch", "--quiet", "origin"],
        cwd=str(project_root),
        capture_output=True,
        timeout=5,
    )
    for branch in ("origin/main", "origin/master"):
        r = subprocess.run(
            ["git", "rev-parse", "--short", branch],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def _get_local_commit(project_root: "Path") -> str:
    """Return the short hash of the local HEAD."""
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _run_background_update(project_root: "Path") -> None:
    """Spawn a background process that pulls the latest code and reinstalls.

    The background process writes its result to a status file so the next
    SessionStart can report success or failure.
    """
    status_file = _UPDATE_CHECK_STATE_FILE.parent / "bg_update_status.json"

    # Build the update script as a single Python command string so we do not
    # need a separate helper file on disk.
    update_script = r"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

project_root = sys.argv[1]
status_file = sys.argv[2]

def run(args, **kw):
    return subprocess.run(args, cwd=project_root, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=30, **kw)

errors = []

# 1. git pull
r = run(["git", "pull", "--ff-only"])
if r.returncode != 0:
    errors.append(f"git pull failed: {r.stderr.strip()}")

# 2. pip install -e .
if not errors:
    r = run([sys.executable, "-m", "pip", "install", "-e", ".", "-q"])
    if r.returncode != 0:
        errors.append(f"pip install failed: {r.stderr.strip()}")

# Get new HEAD commit hash
r2 = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    cwd=project_root,
    capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=5,
)
new_commit = r2.stdout.strip() if r2.returncode == 0 else "unknown"

result = {
    "completed_at": time.time(),
    "success": len(errors) == 0,
    "new_commit": new_commit,
    "errors": errors,
}
Path(status_file).write_text(json.dumps(result), encoding="utf-8")
"""

    try:
        subprocess.Popen(
            [sys.executable, "-c", update_script, str(project_root), str(status_file)],
            # Detach from the parent process completely so it survives hook timeout
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        pass


def _check_for_updates() -> str | None:
    """Check if a newer version is available on git remote; auto-update in background.

    Uses a 24-hour cooldown to avoid triggering on every session start.
    """
    _UPDATE_CHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # --- Report result of a previously-started background update ---
    bg_status_file = _UPDATE_CHECK_STATE_FILE.parent / "bg_update_status.json"
    if bg_status_file.exists():
        try:
            bg = json.loads(bg_status_file.read_text(encoding="utf-8"))
            bg_status_file.unlink(missing_ok=True)
            if bg.get("success"):
                new_commit = bg.get("new_commit", "unknown")
                _UPDATE_CHECK_STATE_FILE.write_text(
                    json.dumps({"last_checked": time.time(), "notice": None}),
                    encoding="utf-8",
                )
                return f"[OS] Automatically updated to the latest version (commit: {new_commit})"
            else:
                errs = "; ".join(bg.get("errors", ["unknown error"]))
                return f"[OS] Auto-update failed: {errs}"
        except Exception:
            pass

    # --- Cooldown check ---
    try:
        if _UPDATE_CHECK_STATE_FILE.exists():
            state = json.loads(_UPDATE_CHECK_STATE_FILE.read_text(encoding="utf-8"))
            last_checked = state.get("last_checked", 0)
            if time.time() - last_checked < _UPDATE_CHECK_COOLDOWN_SECS:
                return state.get("notice")
    except Exception:
        pass

    # --- Locate project root ---
    project_root = _resolve_project_root()

    notice: str | None = None

    if project_root is not None:
        # Run the blocking git fetch+compare in a thread with a hard 2s timeout
        # so it never delays the bootstrap past the hook timeout.
        def _check_git_update(root: "Path") -> "str | None":
            try:
                local_commit = _get_local_commit(root)
                remote_commit = _get_remote_commit(root)
                if local_commit and remote_commit and local_commit != remote_commit:
                    # Only update when the remote is strictly ahead. A local lead/divergence (the dev-machine norm:
                    # committed but not pushed)
                    # must not trigger it — otherwise every session would falsely report "a new version was detected"
                    # and run git pull for nothing.
                    anc = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", remote_commit, "HEAD"],
                        cwd=str(root),
                        capture_output=True,
                        timeout=3,
                    )
                    if anc.returncode == 0:
                        return None  # the remote commit is already in the local history → the local side is not
                        # behind; no update needed
                    _run_background_update(root)
                    return (
                        f"[OS] A new version was detected (local: {local_commit} → remote: {remote_commit}), "
                        "auto-updating in the background; it takes effect on the next startup."
                    )
            except Exception:
                pass
            return None

        try:
            with ThreadPoolExecutor(max_workers=1) as _ex:
                future = _ex.submit(_check_git_update, project_root)
                notice = future.result(timeout=2.0)
        except (FuturesTimeoutError, Exception):
            # Timed out or failed — skip update check silently
            pass

    try:
        _UPDATE_CHECK_STATE_FILE.write_text(
            json.dumps({"last_checked": time.time(), "notice": notice}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return notice


_DISMISSED_PROJECTS_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"


def _normalize_cwd(cwd: str) -> str:
    """Normalize a path for comparison: resolve, lowercase, forward slashes."""
    return str(Path(cwd).resolve()).replace("\\", "/").lower()


def _load_dismissed_projects() -> list[str]:
    """Load the list of dismissed cwd paths from the dismissed_projects.json file."""
    try:
        if _DISMISSED_PROJECTS_FILE.exists():
            data = json.loads(_DISMISSED_PROJECTS_FILE.read_text(encoding="utf-8"))
            return data.get("dismissed", [])
    except Exception:
        pass
    return []


def _check_project_registration(api_url: str, cwd: str) -> tuple[bool, bool, dict]:
    """Check if the current cwd is registered as an OS project.

    Args:
        api_url: Base API URL
        cwd: Current working directory path

    Returns:
        Tuple of (is_registered, is_dismissed, project_info)
        - is_registered: True if cwd matches a project in the OS
        - is_dismissed: True if user previously dismissed registration for this cwd
        - project_info: Project dict if registered, empty dict otherwise
    """
    cwd_norm = _normalize_cwd(cwd)

    # Check dismissed list first (no API call needed)
    dismissed = _load_dismissed_projects()
    is_dismissed = cwd_norm in dismissed

    # Call /api/context/resolve with auto_create=false to check registration
    try:
        payload = json.dumps({"cwd": cwd, "auto_create": False}).encode("utf-8")
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            project_id = result.get("project_id") or (result.get("project", {}) or {}).get("id", "")
            if project_id:
                project_info = result.get("project") or {"id": project_id}
                return True, is_dismissed, project_info
    except Exception:
        # API unreachable or endpoint missing — fall back to projects list matching
        pass

    return False, is_dismissed, {}


def _check_teams_dir_cleanup() -> str | None:
    """Scan ~/.claude/teams/ and warn if too many team directories accumulate."""
    teams_dir = Path.home() / ".claude" / "teams"
    if not teams_dir.exists():
        return None
    try:
        team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
        count = len(team_dirs)
        if count > 10:
            # The TeamDelete tool no longer exists in CC v2.1.219 (tool surface checked 2026-07-25);
            # implicit team directories are built and managed by CC itself — only hint at manual cleanup.
            return (
                f"[OS Reminder] Detected {count} historical session team directories, which can be cleaned up manually:"
                "rm -rf ~/.claude/teams/<old directory> (do not delete the current session's directory)"
            )
    except Exception:
        pass
    return None


def _build_unregistered_briefing(cwd: str, is_dismissed: bool) -> str:
    """The minimal briefing for unregistered directories (root fix for the 2026-07-21 cross-wiring incident).

    Outputs only: 1) an identity note + registration/ignore guidance; 2) the legitimate global discipline (global/user
    buckets) + this directory's
    fingerprint temporary-bucket memories. **Deliberately does not output** team lists, historical team directory
    cleanup reminders, Leader rules,
    the task wall, auto wake/loop, skills/templates — those are registered-project workbench content and are noise to
    unrelated directories
    (incident record: a session in an unregistered directory was poured full of another project's framework text)."""
    lines = []
    lines.append("[AI Team OS] The current directory is not registered as an OS project — Minimal briefing")
    lines.append("")
    if not is_dismissed:
        dir_name = Path(cwd).name or cwd
        lines.append("⚠️ Not registered in the AI Team OS project system:")
        lines.append(f"   {cwd}")
        lines.append("")
        lines.append("After registering, you can use the task wall/meetings/reports/Dashboard with project "
                     "isolation; if not registered, this session")
        lines.append("inherits only the global discipline + this directory's temporary memories and does not "
                     "participate in any team workbench.")
        lines.append("")
        lines.append(f"→ When the user says \"register\"/\"yes\"/\"ok\" → execute: "
                     f"project_create(name='{dir_name}', root_path='{cwd}')")
        lines.append("→ When the user says \"no need\"/\"don't register\"/\"no\" → execute: "
                     "dismiss_project_registration(cwd='" + cwd + "')")
        lines.append("")
    # Direction memories: the global/user genuinely-global discipline + this directory's fingerprint temporary bucket
    # (derived by the API from X-Project-Dir).
    # Inherits only these — never other projects' project memories. When the API is unreachable, silently empty.
    try:
        mem_items = _fetch_direction_memories(project_dir=cwd)
        lines.extend(_render_direction_memories(mem_items, budget=_MEM_INJECT_FUSE))
    except Exception:
        pass
    return "\n".join(lines)


def _build_briefing() -> str:
    """Build Leader briefing (registered project) or minimal briefing (unregistered dir)."""
    # 0. Resolve cwd for project matching
    cwd = os.getcwd().replace("\\", "/")

    # Parallel fetch: projects, teams, briefings (task-wall needs project_id first)
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_projects = pool.submit(_api_get, "/api/projects")
        f_teams = pool.submit(_api_get, "/api/teams")
        f_briefings = pool.submit(_api_get, "/api/leader-briefings?status=pending")
        projects_data = f_projects.result()
        teams_data = f_teams.result()
        briefings_early = f_briefings.result()

    # Resolve matched project: first try /api/context/resolve, then fall back to list matching
    is_registered, is_dismissed, reg_project_info = _check_project_registration(API_URL, cwd)

    # If context/resolve didn't confirm registration, fall back to already-fetched projects list
    matched_project_id = ""
    if is_registered:
        matched_project_id = (reg_project_info.get("id") or "")
    elif projects_data and projects_data.get("data"):
        # Longest-prefix match — pick most specific project when several match
        best_proj = None
        best_len = -1
        cwd_lower = cwd.rstrip("/").lower()
        for proj in projects_data["data"]:
            rp = (proj.get("root_path") or "").replace("\\", "/").rstrip("/")
            if rp and cwd_lower.startswith(rp.lower()) and len(rp) > best_len:
                best_proj = proj
                best_len = len(rp)
        if best_proj is not None:
            is_registered = True
            matched_project_id = best_proj.get("id", "")

    # Unregistered directory: the minimal briefing, and return here — do not pour in workbench content such as
    # teams/rules/the task wall/wakes/skills.
    if not is_registered:
        return _build_unregistered_briefing(cwd, is_dismissed)

    # ===== Registered project: the full Leader briefing (the content below is verbatim-identical to the old version;
    # only the registration check moved earlier)=====
    lines = []
    lines.append("[AI Team OS] Session started — Leader briefing")
    lines.append("")

    # Team directory cleanup reminder
    cleanup_notice = _check_teams_dir_cleanup()
    if cleanup_notice:
        lines.append(cleanup_notice)
        lines.append("")

    # Update availability notice (24h cooldown, non-blocking)
    update_notice = _check_for_updates()
    if update_notice:
        lines.append(f"[UPDATE] {update_notice}")
        lines.append("")

    # Fetch task-wall once (used for both top5 and in-progress sections)
    wall_data = None
    if matched_project_id:
        wall_data = _api_get(f"/api/projects/{matched_project_id}/task-wall?limit=20&include_completed=false")

    # 1. Team status
    if teams_data and teams_data.get("data"):
        teams = teams_data["data"]
        active = [t for t in teams if t.get("status") == "active"]
        completed = [t for t in teams if t.get("status") == "completed"]
        lines.append(f"Team: {len(active)} active, {len(completed)} completed")
        for t in active:
            lines.append(f"  - {t['name']} (active)")
    else:
        lines.append("Team: none yet")

    lines.append("")

    # 2. Top tasks from task wall (single fetched result reused below)
    if wall_data and wall_data.get("wall"):
        wall = wall_data["wall"]
        pending = []
        for horizon in ["short", "mid", "long"]:
            for task in wall.get(horizon, []):
                pending.append(task)
        pending.sort(key=lambda t: t.get("score", 0), reverse=True)
        if pending:
            lines.append("Task Wall Top 5:")
            for t in pending[:5]:
                priority = t.get("priority", "medium")
                horizon = t.get("horizon", "mid")
                score = t.get("score", 0)
                lines.append(f"  [{priority}/{horizon}] {t['title']} (score:{score:.1f})")
        else:
            lines.append("Task wall: no to-do tasks")
        lines.append("")

        stats = wall_data.get("stats", {})
        if stats:
            lines.append(
                f"Stats: {stats.get('total', 0)} tasks in total, "
                f"{stats.get('completed_count', 0)} completed, "
                f"{stats.get('by_status', {}).get('pending', 0)} to-do"
            )
            lines.append("")

    # 3. Rule reminders — top 5 critical rules only (full rules: GET /api/system/rules)
    lines.append("=== Leader Core Rules (Top 5) ===")
    lines.append("1. Focus on coordination: dispatch implementation work to members and only coordinate yourself. "
                 "The Agent team_name parameter is deprecated — do not pass it; dispatched agents are automatically "
                 "absorbed")
    lines.append("2. Do not wait idle after dispatching: take the next task and advance in parallel (at most 3 "
                 "directions); when the task wall is empty, use your own judgment — do not invent work just to have "
                 "something to do")
    lines.append("3. Decide autonomously: make tactical decisions (task assignment/implementation approach) "
                 "yourself; only consult the user on strategic decisions (project direction/major architecture)")
    lines.append("4. Protect progress: record progress with task_memo_add every 2 operations; if the same approach "
                 "fails 3 times, change approach or escalate")
    lines.append("5. Context: save progress when [CONTEXT WARNING] appears; when the user returns, first report a "
                 "phase summary + pending decisions")
    lines.append("→ Full rules: GET /api/system/rules")
    lines.append("")

    # 3.5 Direction-memory section (memory system v2 P1): valid direction-layer entries injected grouped by kind;
    # silently skipped when the API is unreachable
    try:
        mem_items = _fetch_direction_memories(matched_project_id)
        lines.extend(_render_direction_memories(mem_items, budget=_MEM_INJECT_FUSE))
    except Exception:
        pass

    # In-progress task reminders (reuse already-fetched wall_data — no extra API call)
    if wall_data and wall_data.get("wall"):
        in_progress = []
        for horizon in ["short", "mid", "long"]:
            for task in wall_data["wall"].get(horizon, []):
                status = task.get("status", "")
                if status in ("in_progress", "running"):
                    in_progress.append(task)
        if in_progress:
            lines.append("=== In-Progress Tasks ===")
            for t in in_progress:
                assignee = t.get("assigned_to", "Unassigned")
                lines.append(f"  - {t['title']} (assigned: {assignee})")
            lines.append("→ Please check whether these tasks need a status update or a memo added")
            lines.append("")

    # 4. Pending Leader Briefings (reuse already-fetched briefings_early)
    briefings = briefings_early
    if briefings and briefings.get("data"):
        items = briefings["data"]
        if items:
            lines.append(f"=== Leader briefing: {len(items)} pending decisions ===")
            for b in items[:5]:
                lines.append(f"  [{b.get('urgency','medium')}] {b['title']}")
                if b.get('recommendation'):
                    lines.append(f"    Recommendation: {b['recommendation'][:60]}")
            lines.append("→ When the user steps in, first report the pending decisions above; use briefing_list for "
                         "details")
            lines.append("")

    # 5. Auto-wake instruction (v2: event-driven + dynamic interval, replaces 30min cron)
    # Wake system v2, see docs/wake-loop-v2-design.md §5: no longer instructs building a fixed 30-minute cron;
    # instead, run /loop once (dynamic interval); the OS maintenance hints are carried by ~/.claude/loop.md.
    lines.append("=== Auto Wake ===")
    # ③ Nagging items become conditional sentences: an unconditional "please run /loop" is a wrong instruction for a
    # session focused on a single task (empirically it gets
    # systematically ignored). Recommend /loop only to sessions carrying coordination duties; single-task sessions may
    # ignore it.
    lines.append("If this session carries coordination duties (Leader/coordinator), running /loop once is "
                 "recommended; sessions focused on a single task may ignore this.")
    lines.append("(No interval = dynamic: each round chooses its own 1-60 minute delay — tighter when there is "
                 "work, longer when idle.) See ~/.claude/loop.md for what to do each round.")
    lines.append("")

    # Auto team creation instructions
    team_config = _load_team_config()
    if team_config:
        lines.extend(_build_auto_team_instructions(team_config))

    return "\n".join(lines)


def _fetch_compact_checkpoint(session_id: str) -> str:
    """Get the injectable text of this session's most recent compaction checkpoint; return an empty string when there
    is none.

    Deliberately no fallback copy: when there is no checkpoint, inject nothing — never occupy the user's context with
    a placeholder.
    """
    if not session_id:
        return ""
    try:
        data = _api_get(f"/api/hooks/compact-checkpoint?session_id={session_id}")
        if isinstance(data, dict) and data.get("found"):
            return str(data.get("text") or "")
    except Exception:
        pass
    return ""


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    # Read session info from stdin
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        session_info = json.loads(raw) if raw.strip() else {}
    except Exception:
        session_info = {}

    _cleanup_stale_subagent_markers()

    # Check if API is reachable (1 retry with short sleep — keeps us under 3s hook timeout)
    health = _api_get("/api/teams")
    if health is None:
        time.sleep(0.3)
        health = _api_get("/api/teams")

    if health is not None:
        # API reachable -> output briefing to stdout (injected into Claude context)
        briefing = _build_briefing()
        sys.stdout.write(briefing)

        # Compaction recovery path (Q6 ruling A): SessionStart's source has five values
        # startup/resume/clear/compact/fork, and only compact means "the previous round's
        # context was just compacted away". At that point, hand back the OS-side operational state PreCompact stored,
        # as-is —
        # CC's own compact_summary is already in the model's context after compaction; what the OS should supply is
        # the half the model
        # does not have (in-flight agents / unfinished tasks / items awaiting a ruling).
        compact_block = ""
        if session_info.get("source") == "compact":
            compact_block = _fetch_compact_checkpoint(session_info.get("session_id", ""))
            if compact_block:
                sys.stdout.write(compact_block)

        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API reachable at {API_URL}\n"
            f"[aiteam-bootstrap] session_id={session_info.get('session_id', 'unknown')}\n"
            f"[aiteam-bootstrap] briefing injected ({len(briefing)} chars)"
            + (f" + compact checkpoint ({len(compact_block)} chars)" if compact_block else "")
            + "\n"
        )
    else:
        # API not reachable
        # D3 stage D bleeding-stop (audit M50): no longer guide starting a second uvicorn instance manually — that
        # would coexist with
        # the MCP auto-started instance, causing dual reapers/duplicate wakes. The API is launched automatically with
        # the MCP startup.
        sys.stdout.write(
            "[AI Team OS] The API is not running. The API is launched automatically with the MCP server:\n"
            "1) Restart Claude Code (recommended; confirm ai-team-os is connected via /mcp);\n"
            "2) Or call the MCP tool os_restart_api from a connected session.\n"
            "Do not manually run uvicorn to start a second instance (it would coexist with the auto-started instance "
            "and cause duplicate wakes).\n"
        )
        sys.stderr.write(f"[aiteam-bootstrap] AI Team OS API not reachable at {API_URL}\n")


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
