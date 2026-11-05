"""Project and phase management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.clock import utc_now
from aiteam.mcp._base import _api_call, _resolve_project_id


def register(mcp):
    """Register all project-related MCP tools."""

    @mcp.tool()
    def project_create(
        name: str,
        description: str = "",
        root_path: str = "",
    ) -> dict[str, Any]:
        """Create a new project with a default Phase automatically created.

        ⚠️ IMPORTANT: Projects are automatically registered by the OS when
        a CC session starts. You should NOT manually create projects unless
        the auto-registered project is missing. The root_path MUST match
        the current CC session's working directory — do NOT create projects
        pointing to other directories.

        Args:
            name: Project name
            description: Project description
            root_path: Project root directory path (must match current cwd)

        Returns:
            Created project info including project_id
        """
        import os

        cwd = os.getcwd().replace("\\", "/")
        if root_path:
            given = root_path.replace("\\", "/").rstrip("/")
            cwd_norm = cwd.rstrip("/")
            g = given.lower()
            c = cwd_norm.lower()
            # Separator boundary (following _base._init_session_project's approach): a bare startswith
            # is a false prefix — root_path='/Users/cron' would fool cwd='/Users/cronus/...',
            # making it possible to register a project on someone else's directory, colliding with the ownership iron
            # rule.
            if not (c == g or c.startswith(g + "/")):
                return {
                    "success": False,
                    "error": (
                        f"root_path '{root_path}' does not match current "
                        f"working directory '{cwd}'. Projects must be "
                        f"created for the current session directory. "
                        f"The OS auto-registers projects on session start "
                        f"— use project_list to find existing projects."
                    ),
                    "_recovery": "Use project_list to find auto-registered projects.",
                }
            # A strict ancestor of the home directory ('/', '/Users' and the like) is never a legitimate project root:
            # after registration it would
            # prefix-claim every unregistered directory from then on — one project swallowing the whole machine.
            home = os.path.expanduser("~").replace("\\", "/").rstrip("/").lower()
            if home and g != home and (home == g or home.startswith(g + "/")):
                return {
                    "success": False,
                    "error": (
                        f"root_path '{root_path}' is an ancestor directory of the home directory and cannot be a "
                        f"project root "
                        f"(it would prefix-claim every unregistered directory from then on). "
                        f"Please use this session's real working directory '{cwd}'."
                    ),
                    "_recovery": "Use the session cwd as root_path, or project_list to find it.",
                }

        result = _api_call(
            "POST",
            "/api/projects",
            {
                "name": name,
                "description": description,
                "root_path": root_path or cwd,
            },
        )
        # Registration-regularization hint (the memory-isolation upgrade path): the direction memories this directory
        # wrote during its unregistered period landed in
        # the directory-fingerprint temporary bucket ("dir:<sha1>"); after regularization these entries can be
        # migrated into the project bucket. This batch does no automatic
        # absorption — only the hint; if migration is needed, handle it manually with memory_reconcile's promote/merge.
        if isinstance(result, dict) and result.get("success") is not False:
            result.setdefault(
                "hint",
                "The temporary-bucket memories this directory wrote during its earlier unregistered period "
                "(dir:<fingerprint>, if any) can be migrated into this project bucket — "
                "this batch does not absorb them automatically; if migration is needed, please handle it manually "
                "with memory_reconcile.",
            )
        return result

    @mcp.tool()
    def project_list() -> dict[str, Any]:
        """List all projects in the system.

        Returns:
            projects: List of all projects with id, name, description, root_path, etc.
        """
        return _api_call("GET", "/api/projects")

    @mcp.tool()
    def project_update(
        project_id: str,
        name: str = "",
        description: str = "",
        root_path: str = "",
    ) -> dict[str, Any]:
        """Update a project's name, description, or root_path.

        Args:
            project_id: Project ID to update
            name: New project name (optional)
            description: New description (optional)
            root_path: New root directory path (optional)

        Returns:
            Updated project info
        """
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if root_path:
            body["root_path"] = root_path
        if not body:
            return {"success": False, "error": "No fields to update"}
        return _api_call("PUT", f"/api/projects/{project_id}", body)

    @mcp.tool()
    def project_delete(project_id: str) -> dict[str, Any]:
        """Delete a project.

        Args:
            project_id: Project ID to delete

        Returns:
            Deletion result
        """
        return _api_call("DELETE", f"/api/projects/{project_id}")

    @mcp.tool()
    def project_summary(project_id: str = "") -> dict[str, Any]:
        """Get a quick project summary: status (active/inactive), teams, top tasks.

        Args:
            project_id: Project ID (optional, auto-uses active project if empty)

        Returns:
            Project status, active team count, pending/running task counts, top 3 tasks
        """
        resolved = _resolve_project_id(project_id)
        if not resolved:
            return {"success": False, "error": "No project context"}
        return _api_call("GET", f"/api/projects/{resolved}/summary")

    @mcp.tool()
    def dismiss_project_registration(cwd: str = "") -> dict[str, Any]:
        """Mark current cwd as dismissed for project registration — won't ask again.

        Args:
            cwd: Directory path to dismiss (empty = use current cwd)

        Returns:
            Status dict with dismissed_count and normalized cwd
        """
        import json as _json
        import os as _os
        from pathlib import Path as _Path

        if not cwd:
            cwd = _os.getcwd()
        cwd_norm = str(_Path(cwd).resolve()).replace("\\", "/").lower()

        dismissed_file = _Path.home() / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"
        dismissed_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            data = (
                _json.loads(dismissed_file.read_text(encoding="utf-8"))
                if dismissed_file.exists()
                else {"dismissed": []}
            )
        except Exception:
            data = {"dismissed": []}

        if cwd_norm not in data["dismissed"]:
            data["dismissed"].append(cwd_norm)
        data["updated_at"] = utc_now().isoformat()

        dismissed_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "dismissed_count": len(data["dismissed"]), "cwd": cwd_norm}
