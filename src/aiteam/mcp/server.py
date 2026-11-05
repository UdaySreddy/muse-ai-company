"""AI Team OS — MCP Server.

Provides MCP tools that call corresponding API endpoints on the local
FastAPI server (localhost:8000) via HTTP.
MCP Server runs in stdio mode, fully decoupled from the FastAPI process.

Tools are organized in src/aiteam/mcp/tools/ submodules and registered
via register_all(mcp) at import time.
"""

from __future__ import annotations

# fastmcp 3.x by default connects to PyPI at startup to check for its own new version; on a machine with a SOCKS proxy
# set (e.g. Clash)
# and no socksio installed, that check blows up the whole stdio server with an ImportError — on the CC side it appears
# as "-32000 reconnect failed". The governance layer should not reach the external network on the startup path; turn
# it off directly.
# (Change the settings attribute at runtime rather than an environment variable: settings are frozen when fastmcp is
# imported.)
import fastmcp  # noqa: E402
from fastmcp import FastMCP

import aiteam

# Auto-start infrastructure — extracted to _autostart.py
from aiteam.mcp._autostart import (  # noqa: F401
    _cleanup_api,
    _ensure_api_running,
    _get_running_api_version,
    _is_api_healthy,
    _is_port_open,
    _kill_port_occupant,
    _read_pid_file,
    _write_pid_file,
)

# Shared infrastructure — extracted to _base.py
from aiteam.mcp._base import (  # noqa: F401
    API_URL,
    PROJECT_DIR,
    _api_call,
    _init_session_project,
    _resolve_project_id,
    _resolve_team_id,
    _session_project_id,
    logger,
)

fastmcp.settings.check_for_updates = "off"

# The instructions are tool search's only server-level description — whether the 112 tools can be found is decided by
# this text,
# so it is organized by "capability → entry tool" rather than listing nouns. The body lives in _instructions.py (a
# long literal kept as data).
from aiteam.mcp._instructions import INSTRUCTIONS  # noqa: E402

# The version must be passed explicitly: without it, FastMCP puts **fastmcp's own version** into the initialize
# response's serverInfo.version, and the client-side version gates/telemetry would see the framework version instead
# of the OS
# version. Here it reads aiteam.__version__ at runtime; it is not a new version-number source of truth (the five
# places in the I2 lockstep remain
# the five literals), naturally following src/aiteam/__init__.py.
mcp = FastMCP(
    name="ai-team-os",
    version=aiteam.__version__,
    instructions=INSTRUCTIONS,
)

# Register all tools from submodules
from aiteam.mcp.tools import register_all  # noqa: E402

register_all(mcp)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    _ensure_api_running()
    _init_session_project()
    # Tool progressive loading P1: after the API is ready, attach the alwaysLoad meta to recent high-frequency tools
    # to exempt them from defer.
    # Best-effort: API absent/timeout silently degrades to all defer (see _alwaysload.apply_always_load_meta).
    from aiteam.mcp._alwaysload import apply_always_load_meta

    apply_always_load_meta(mcp)
    mcp.run()
