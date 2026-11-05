"""Shared infrastructure for AI Team OS MCP tools.

Contains the HTTP helper, project/team resolvers, and global state
that all tool modules depend on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aiteam.mcp._error_recovery import get_business_recovery, get_connection_recovery, get_http_recovery

logger = logging.getLogger(__name__)

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")


def _get_api_port() -> int:
    """Read port from port file. Returns 8000 if file missing or invalid."""
    try:
        return int(open(_PORT_FILE).read().strip())
    except (FileNotFoundError, ValueError):
        return 8000


def _get_api_url() -> str:
    """Return the current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    return f"http://localhost:{_get_api_port()}"


# Module-level alias for backwards compatibility (used in _autostart import guard)
API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
# Project directory for DB isolation — set by Claude Code environment
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "")

# Process-level project ID — resolved once at startup from cwd → root_path match.
# Safe because each CC session spawns its own MCP server subprocess.
# Set by _init_session_project() after API is ready, before mcp.run().
_session_project_id: str = ""


# ============================================================
# HTTP helper
# ============================================================


def _api_call(
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Unified API call helper using urllib standard library.

    Args:
        method: HTTP method (GET / POST / PUT / DELETE)
        path: API path, e.g., /api/teams
        data: Request body data (used for POST/PUT only)
        extra_headers: Additional headers to merge into the request
            (e.g. {"X-Project-Id": "..."} for ecosystem project scoping).

    Returns:
        API response as a JSON dict
    """
    url = f"{_get_api_url()}{urllib.parse.quote(path, safe='/?&=%')}"
    headers = {"Content-Type": "application/json"}
    if PROJECT_DIR:
        # HTTP headers must be ASCII/latin-1; percent-encode the path so
        # non-ASCII characters (e.g. Chinese directory names) are safe.
        # The API side decodes with urllib.parse.unquote before path matching.
        headers["X-Project-Dir"] = urllib.parse.quote(PROJECT_DIR, safe="/:.-_\\")
    # Stage J: auto-inject session-resolved X-Project-Id (read once at startup
    # via _init_session_project, no recursion risk). Subordinate to extra_headers
    # so callers can still override (e.g. cross-project tools force a different id).
    if _session_project_id:
        headers.setdefault("X-Project-Id", _session_project_id)
    # Attribution v1 §2.4: pass the current CC session id to the server, to resolve the author name in a bookkeeping
    # action
    # to the agent row **within this session's domain**. Without it the server can only search the whole table by name
    # — and name is
    # not unique in the agents table (measured: the single name "Leader" has 117 rows, spanning 79 teams over three
    # weeks), so
    # whole-table matching inevitably mis-binds across time to a same-named row. When the server cannot get this
    # header it simply writes no edge (rather one fewer).
    session_id = _cc_session_id()
    if session_id:
        headers.setdefault("X-CC-Session-Id", session_id)
    if extra_headers:
        headers.update(extra_headers)

    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        recovery_info = get_http_recovery(e.code)
        # Upgrade category from business keywords if body carries more context
        business_category = get_business_recovery(error_body)
        result: dict[str, Any] = {
            "success": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "detail": error_body,
            "_error_category": business_category or recovery_info.get("category", "unknown"),
            "_recovery": recovery_info.get("recovery", ""),
        }
        return result
    except urllib.error.URLError as e:
        reason_str = str(e.reason)
        recovery_info = get_connection_recovery(reason_str)
        return {
            "success": False,
            "error": f"Unable to connect to the AI Team OS API ({_get_api_url()}): {e.reason}",
            "hint": "Please make sure the FastAPI service is running: aiteam serve",
            "_error_category": recovery_info.get("category", "api_unavailable"),
            "_recovery": recovery_info.get("recovery", ""),
        }
    except Exception as e:
        recovery_info = get_connection_recovery(str(e))
        return {
            "success": False,
            "error": f"Request failed: {e!s}",
            "_error_category": recovery_info.get("category", "unknown"),
            "_recovery": recovery_info.get("recovery", ""),
        }


# ============================================================
# Resolvers
# ============================================================


_SESSION_TEAM_NAME_RE = re.compile(r"^session-[0-9a-fA-F]{8}$")


def _cc_session_id() -> str:
    """The current CC session id — CC injects CLAUDE_CODE_SESSION_ID into each MCP server subprocess.

    (Measured on CC v2.1.219: `ps eww <mcp-pid>` shows the variable; one MCP subprocess per session,
    so it is constant over this process's lifetime.)
    """
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        or os.environ.get("CLAUDE_SESSION_ID", "")
    ).strip()


def _team_owned_by_session(team: dict[str, Any], session_id: str) -> bool:
    """Whether the team belongs to this session (same criterion as hook_translator._team_owned_by_session).

    ① config.owner_session_id hits (the authoritative ownership stamp applied at team creation);
    ② legacy-row fallback: the container team name encodes the first 8 characters of the session id
    (``session-<sid8>``).
    """
    if not session_id:
        return False
    owner = (team.get("config") or {}).get("owner_session_id")
    if owner:
        return owner == session_id
    return str(team.get("name") or "") == f"session-{session_id[:8]}"


def _is_workflow_team(team: dict[str, Any]) -> bool:
    """A workflow per-run team: one is created per Workflow run and must never be used as the "current team"."""
    if (team.get("config") or {}).get("kind") == "workflow":
        return True
    return str(team.get("name") or "").startswith("workflow-")


def _is_foreign_session_container(team: dict[str, Any], session_id: str) -> bool:
    """Another session's session-<sid8> container team (this session's was already picked in tier 1)."""
    if (team.get("config") or {}).get("kind") == "session":
        return not _team_owned_by_session(team, session_id)
    return bool(_SESSION_TEAM_NAME_RE.match(str(team.get("name") or "")))


def _team_recency_key(team: dict[str, Any]) -> str:
    """The key for sorting by most recent activity (ISO strings compare lexicographically directly)."""
    return str(team.get("updated_at") or team.get("created_at") or "")


def _newest(teams: list[dict[str, Any]]) -> dict[str, Any]:
    return max(teams, key=_team_recency_key)


def pick_active_team(teams: list[dict[str, Any]], session_id: str, project_id: str) -> dict | None:
    """Pick the "current team" from the active teams, in three priority tiers; return None when all miss.

    Tier 1  this session's session container team (owner_session_id / session-<sid8> hit);
    Tier 2  the most recently active team in this project that is neither a workflow-* per-run team nor another
    session's container team;
    Tier 3  the most recently active team in this project (the fallback when only workflow teams remain).

    The old implementation took ``active_teams[0]`` directly: the teams list is in created_at descending order, and
    among the active teams
    the newest is usually a just-started workflow-* per-run team — so meetings / team knowledge / activity queries
    and every other "empty argument automatically uses the active team" tool all bound to the workflow team (the wrong
    object).

    Cross-project teams are never selected: rather return None and let the caller error explicitly than borrow another
    project's team.
    """
    active = [
        t for t in teams
        if isinstance(t, dict) and t.get("id") and t.get("status") == "active"
    ]
    if not active:
        return None

    owned = [t for t in active if _team_owned_by_session(t, session_id)]
    if owned:
        return _newest(owned)

    # Within the project (including historical rows with no project_id annotation); no limit when the project is
    # unresolved
    in_scope = [
        t for t in active
        if not project_id or not t.get("project_id") or t.get("project_id") == project_id
    ]
    if not in_scope:
        return None

    plain = [
        t for t in in_scope
        if not _is_workflow_team(t) and not _is_foreign_session_container(t, session_id)
    ]
    if plain:
        return _newest(plain)
    return _newest(in_scope)


def _resolve_team_id(team_id: str) -> str:
    """Resolve the "current team" id for an empty argument; return an empty string when it cannot be resolved.

    See :func:`pick_active_team` for the priority. When an empty string is returned, the caller **must** error
    explicitly
    (all current call sites return ``{"success": False, "error": ...}``) — never allow
    falling back to the silent guess of "just pick any active team".

    NOTE: This calls _api_call to get context. The context_resolve MCP tool
    in tools/infra.py has the full implementation; this resolver uses a
    lightweight version to avoid circular imports.
    """
    if team_id:
        return team_id
    try:
        teams_data = _api_call("GET", "/api/teams")
        rows = teams_data.get("data") or [] if isinstance(teams_data, dict) else []
        picked = pick_active_team(rows, _cc_session_id(), _session_project_id)
    except Exception:
        logger.warning("Team resolution failed", exc_info=True)
        return ""
    if picked is None:
        logger.info("No resolvable active team for this session/project")
        return ""
    return str(picked["id"])


def _resolve_project_id(project_id: str) -> str:
    """Resolve project_id: explicit param > session constant > empty.

    _session_project_id is set once at startup from cwd matching.
    No dynamic resolution = no recursion risk.
    """
    if project_id:
        return project_id
    return _session_project_id


# ============================================================
# Session init
# ============================================================


def _init_session_project() -> None:
    """Resolve project_id once from cwd at startup. No recursion possible."""
    global _session_project_id
    try:
        projects = _api_call("GET", "/api/projects")
        cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
        # Longest-prefix match — multiple projects can match via prefix
        # (e.g. C:/Users/TUF and C:/Users/TUF/Desktop/AI...); pick the most specific.
        best_p = None
        best_len = -1
        for p in projects.get("data", []):
            rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                best_p = p
                best_len = len(rp)
        if best_p is not None:
            _session_project_id = best_p["id"]
            logger.info("Session project: %s (%s)", best_p.get("name"), best_p["id"][:8])
            return
        logger.info("No project match for cwd=%s", cwd)
    except Exception as e:
        logger.warning("Failed to resolve session project: %s", e)
