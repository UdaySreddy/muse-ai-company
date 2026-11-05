"""Batch 3 (5): project_create's root_path validation let through directories it should not have.

Two independent vulnerabilities, both colliding with the 'ownership iron law: the session startup directory governs':

1. ``cwd.startswith(given)`` is a bare prefix comparison with no separator boundary: cwd=
   /Users/cronus/Desktop/AI team OS, so root_path='/Users/cron' could also pass
   (half a path segment). The fix copies _base.py's ``cwd == rp or cwd.startswith(rp + "/")``.
2. After separator alignment, '/Users' is still a legitimate ancestor of the cwd, so it would still pass — whereas
registering an ancestor of the home directory
   as a project root amounts to swallowing the whole machine with one project (afterwards, any unregistered directory
   would be prefix-
   claimed). Therefore one more rule is added: a project root must not be a **strict ancestor** of the home directory.
"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp.tools.project as project_tools

CWD = "/Users/cronus/Desktop/AI team OS"


class _ToolCapture:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _project_create():
    cap = _ToolCapture()
    project_tools.register(cap)
    return cap.tools["project_create"]


def _call(root_path: str, cwd: str = CWD):
    tool = _project_create()
    with patch.object(project_tools, "_api_call", return_value={"success": True}) as api, \
         patch("os.getcwd", return_value=cwd):
        result = tool("demo", "", root_path)
    return result, api


class TestRootPathBoundary:
    def test_partial_segment_rejected(self):
        """Half a path segment (/Users/cron with respect to /Users/cronus) — the old implementation let it through."""
        result, api = _call("/Users/cronus/Desk")
        assert result["success"] is False
        api.assert_not_called()

    def test_partial_segment_rejected_windows(self):
        result, api = _call("C:/Users/TU", cwd="C:/Users/TUF/Desktop/demo")
        assert result["success"] is False
        api.assert_not_called()

    def test_home_ancestor_rejected(self, monkeypatch):
        """/Users (a strict ancestor of the home directory) — the old implementation let it through, equivalent to
        one project swallowing the whole machine."""
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, api = _call("/Users")
        assert result["success"] is False
        api.assert_not_called()

    def test_filesystem_root_rejected(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, api = _call("/")
        assert result["success"] is False
        api.assert_not_called()

    def test_exact_match_accepted(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call(CWD)
        assert result.get("success") is not False

    def test_true_parent_of_cwd_accepted(self, monkeypatch):
        """A true ancestor directory (separator-aligned, and not above the home directory) is still allowed —
        starting a session in a repository subdirectory is a normal scenario."""
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call("/Users/cronus/Desktop", cwd=CWD)
        assert result.get("success") is not False

    def test_case_insensitive_match_still_accepted(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call("/users/CRONUS/desktop", cwd=CWD)
        assert result.get("success") is not False

    def test_empty_root_path_uses_cwd(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        tool = _project_create()
        with patch.object(project_tools, "_api_call", return_value={"success": True}) as api, \
             patch("os.getcwd", return_value=CWD):
            tool("demo", "", "")
        assert api.call_args[0][2]["root_path"] == CWD
