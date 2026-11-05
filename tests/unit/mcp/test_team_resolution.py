"""Batch 3 (1)(2): empty-argument team resolution 'took the wrong object' + context_resolve's plural teams / dead loop
block.

Reproduction of the old, wrong behavior (red → green):
- When ``_resolve_team_id`` got an empty argument it silently took ``active_teams[0]``. Active teams perennially sit
side by side with
  workflow-* per-run teams (one is created per Workflow run, and they are the newest), so meetings /
  team knowledge / activity queries and every other tool that 'automatically uses the active team' all bound to a
  workflow team.
- ``team_close`` / ``team_delete`` also go through the same empty-argument resolution — an irreversible action like
deleting a team
  must never rely on guessing.
- ``context_resolve`` only returned a singular ``team`` (when a project has several teams side by side, the other ones
are invisible),
  and the ``loop`` block measured as a dead placeholder, constantly phase=idle/cycle=0 (loop orchestration has been
  retired).
"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp._base as base
import aiteam.mcp.tools.infra as infra
import aiteam.mcp.tools.team as team_tools

SID = "4d04341d-1398-4c4b-bd71-e71ade52b82b"
SID8 = SID[:8]
PROJ = "3aa58dc7-a771-4745-8fa6-efbd5819956a"
OTHER_PROJ = "99999999-9999-9999-9999-999999999999"


class _ToolCapture:
    """Minimal mock: captures the functions registered via @mcp.tool()."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _team(
    tid: str,
    name: str,
    *,
    project_id: str | None = PROJ,
    status: str = "active",
    config: dict | None = None,
    created_at: str = "2026-07-27T10:00:00",
) -> dict:
    return {
        "id": tid,
        "name": name,
        "project_id": project_id,
        "status": status,
        "config": config or {},
        "created_at": created_at,
        "updated_at": created_at,
    }


def _teams_response(*rows: dict) -> dict:
    return {"success": True, "data": list(rows), "total": len(rows)}


# ---------------------------------------------------------------------------
# (1) _resolve_team_id three-tier priority
# ---------------------------------------------------------------------------


class TestResolveTeamIdPriority:
    def test_session_container_beats_newest_workflow_team(self, monkeypatch):
        """Tier (1): this session's session-<sid8> container team takes priority over a newer workflow-* per-run team.

        The old implementation returned active_teams[0] (the list is in descending created_at order → the newest
        workflow team),
        which is exactly the cause of 'meetings mis-bound to workflow teams'.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_abc123", created_at="2026-07-27T12:00:00"),
            _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "sess-1"

    def test_owner_session_id_is_authoritative(self, monkeypatch):
        """Tier (1): the team name is arbitrary, but a config.owner_session_id hit on this session makes it the
        container team."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_abc123", created_at="2026-07-27T12:00:00"),
            _team(
                "own-1",
                "ai-team-os-dev",
                config={"kind": "session", "owner_session_id": SID},
                created_at="2026-07-27T08:00:00",
            ),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "own-1"

    def test_other_session_container_not_picked(self, monkeypatch):
        """Another session's session-* container team counts neither as tier (1), and tier (2) must also yield to
        ordinary teams (to prevent cross-talk)."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("sess-other", "session-a78b767c", created_at="2026-07-27T12:00:00"),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-27T11:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_non_workflow_project_team_beats_workflow(self, monkeypatch):
        """Tier (2): with no container team for this session, a non-workflow-* active team within this project takes
        priority."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-1", "workflow-wf_zzz", created_at="2026-07-27T12:00:00"),
            _team("wf-2", "workflow-wf_yyy", created_at="2026-07-27T11:30:00"),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-26T10:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_workflow_kind_config_also_excluded(self, monkeypatch):
        """The workflow-team criterion also recognizes config.kind=workflow (it holds even if the team name was
        changed)."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team(
                "wf-1",
                "Some run",
                config={"kind": "workflow"},
                created_at="2026-07-27T12:00:00",
            ),
            _team("dev-1", "ai-team-os-dev", created_at="2026-07-26T10:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "dev-1"

    def test_newest_active_as_last_resort(self, monkeypatch):
        """Tier (3): only when nothing but workflow teams remains in this project does it fall back to 'the newest
        active team'."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("wf-old", "workflow-wf_old", created_at="2026-07-20T10:00:00"),
            _team("wf-new", "workflow-wf_new", created_at="2026-07-27T12:00:00"),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == "wf-new"

    def test_cross_project_team_never_borrowed(self, monkeypatch):
        """A cross-project team must never impersonate this project's context — when everything misses, return an
        empty string so the caller errors explicitly."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(
            _team("other-1", "Another project's team", project_id=OTHER_PROJ),
        )
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == ""

    def test_no_active_team_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)
        rows = _teams_response(_team("done-1", "Old team", status="completed"))
        with patch.object(base, "_api_call", return_value=rows):
            assert base._resolve_team_id("") == ""

    def test_explicit_team_id_short_circuits(self, monkeypatch):
        """Passing an argument explicitly does not query the API (saving an HTTP call, and it cannot take the wrong
        one)."""
        with patch.object(base, "_api_call") as call:
            assert base._resolve_team_id("explicit-id") == "explicit-id"
            call.assert_not_called()

    def test_api_failure_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        with patch.object(base, "_api_call", return_value={"success": False, "error": "boom"}):
            assert base._resolve_team_id("") == ""


# ---------------------------------------------------------------------------
# (1) Dangerous actions forbid empty-argument resolution
# ---------------------------------------------------------------------------


class TestDestructiveToolsRequireExplicitTeam:
    def _tools(self) -> dict:
        cap = _ToolCapture()
        team_tools.register(cap)
        return cap.tools

    def test_team_delete_refuses_empty_team_id(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call") as call:
            result = tools["team_delete"]("")
        assert result["success"] is False
        call.assert_not_called()

    def test_team_close_refuses_empty_team_id(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call") as call:
            result = tools["team_close"]("")
        assert result["success"] is False
        call.assert_not_called()

    def test_destructive_tools_do_not_auto_resolve(self):
        """Even the resolution function must not be imported — empty-argument resolution is completely disabled on
        these two tools."""
        import inspect

        src = inspect.getsource(team_tools)
        close_src = src.split("def team_close")[1].split("@mcp.tool()")[0]
        delete_src = src.split("def team_delete")[1].split("@mcp.tool()")[0]
        assert "_resolve_team_id(" not in close_src
        assert "_resolve_team_id(" not in delete_src

    def test_team_delete_with_explicit_id_still_works(self):
        tools = self._tools()
        with patch.object(team_tools, "_api_call", return_value={"success": True}) as call:
            result = tools["team_delete"]("team-42")
        assert result["success"] is True
        call.assert_called_once_with("DELETE", "/api/teams/team-42")


# ---------------------------------------------------------------------------
# (2) context_resolve: plural teams[] + deleting the dead loop block
# ---------------------------------------------------------------------------


class TestContextResolve:
    def _call(self, monkeypatch, teams: list[dict], cwd: str = "/repo/demo"):
        cap = _ToolCapture()
        infra.register(cap)
        monkeypatch.setattr(infra.os, "getcwd", lambda: cwd)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setattr(base, "_session_project_id", PROJ)

        projects = {
            "success": True,
            "data": [{"id": PROJ, "name": "demo", "root_path": "/repo/demo"}],
        }

        def fake_api(method: str, path: str, *args, **kwargs):
            if path.startswith("/api/projects"):
                return projects
            if path == "/api/teams":
                return {"success": True, "data": teams}
            if path.endswith("/agents"):
                return {"success": True, "data": []}
            if "loop/status" in path:
                raise AssertionError("The loop state block has been retired and should no longer be called")
            return {"success": True, "data": []}

        with patch.object(infra, "_api_call", side_effect=fake_api):
            return cap.tools["context_resolve"]()

    def test_returns_all_active_project_teams(self, monkeypatch):
        result = self._call(
            monkeypatch,
            [
                _team("wf-1", "workflow-wf_abc", created_at="2026-07-27T12:00:00"),
                _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
                _team("other-1", "Another project's team", project_id=OTHER_PROJ),
                _team("done-1", "Old team", status="completed"),
            ],
        )
        names = [t["name"] for t in result["teams"]]
        assert set(names) == {"workflow-wf_abc", f"session-{SID8}"}
        assert "Another project's team" not in names
        assert "Old team" not in names

    def test_singular_team_is_the_session_container(self, monkeypatch):
        """The singular team is kept for compatibility, but it takes the primary team chosen by the three-tier
        priority (no longer the first in the list)."""
        result = self._call(
            monkeypatch,
            [
                _team("wf-1", "workflow-wf_abc", created_at="2026-07-27T12:00:00"),
                _team("sess-1", f"session-{SID8}", created_at="2026-07-27T09:00:00"),
            ],
        )
        assert result["team"]["id"] == "sess-1"

    def test_loop_block_removed(self, monkeypatch):
        """The loop block (a dead placeholder with phase=idle/cycle=0) has been deleted — the key does not exist, and
        that endpoint is no longer hit."""
        result = self._call(
            monkeypatch,
            [_team("sess-1", f"session-{SID8}")],
        )
        assert "loop" not in result

    def test_no_project_means_no_team(self, monkeypatch):
        """When the cwd matches no project, do not borrow another project's team (the ownership iron law)."""
        cap = _ToolCapture()
        infra.register(cap)
        monkeypatch.setattr(infra.os, "getcwd", lambda: "/somewhere/else")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)

        def fake_api(method: str, path: str, *args, **kwargs):
            if path.startswith("/api/projects"):
                return {
                    "success": True,
                    "data": [{"id": PROJ, "name": "demo", "root_path": "/repo/demo"}],
                }
            if path == "/api/teams":
                return {"success": True, "data": [_team("sess-1", f"session-{SID8}")]}
            return {"success": True, "data": []}

        with patch.object(infra, "_api_call", side_effect=fake_api):
            result = cap.tools["context_resolve"]()
        assert result["project"] is None
        assert result["team"] is None
        assert result["teams"] == []
