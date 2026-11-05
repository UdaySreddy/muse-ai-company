"""MCP tool modules — each module exposes a register(mcp) function."""

from __future__ import annotations

import logging
import os

from aiteam.mcp.tools import (
    agent,
    analytics,
    briefing,
    channels,
    ecosystem,
    infra,
    links,
    meeting,
    memory,
    project,
    reports,
    task,
    task_analysis,
    team,
    watchdog,
    workflows,
)
from aiteam.mcp.tools.toolsets import (
    DEFAULT_TOOLSETS,
    WRITE_TOOLS,
    module_enabled,
    resolve_readonly,
    resolve_toolsets,
)

logger = logging.getLogger(__name__)

# Exposed externally for unit-test/documentation reference
__all__ = ["register_all", "DEFAULT_TOOLSETS", "WRITE_TOOLS"]

_MODULES = [
    team,
    agent,
    meeting,
    task,
    project,
    analytics,
    links,
    reports,
    briefing,
    task_analysis,
    memory,
    infra,
    channels,
    watchdog,
    ecosystem,
    workflows,
]

def _remove_write_tools(mcp) -> list[str]:
    """The AITEAM_READONLY tier: after registration, remove the write-class tools and return the actual removed list.

    Write tools are judged by the WRITE_TOOLS explicit list (not guessed by naming pattern). The tool decorator is
    function-level
    registration and cannot select per tool at module register time, so everything registers first and is then removed
    by name.

    Uses only the public ``local_provider.remove_tool`` (FastMCP's top-level method of the same name is, in 3.4.5,
    deprecated, with the official pointer at the provider). Because the names to remove come from our own WRITE_TOOLS
    list, there is no need to enumerate the component table first — the old implementation read the private
    ``provider._components`` for this and imported Tool from
    ``fastmcp.tools.base``; both are certain breakage points when fastmcp 4.0 removes the 3.x compatibility shims.
    Unregistered names (cut by the grouping switch) raise; skipping them is equivalent to "only removing
    those actually registered", with semantics matching the old implementation. Any exception is silently skipped and
    does not block startup.
    """
    removed: list[str] = []
    provider = getattr(mcp, "local_provider", None)
    if provider is None:
        return removed
    for name in sorted(WRITE_TOOLS):
        try:
            provider.remove_tool(name)
        except Exception:
            logger.debug("readonly: write tool %s not registered or removal failed, skipping", name)
            continue
        removed.append(name)
    return removed


def register_all(mcp) -> None:
    """Register tool modules on the given FastMCP instance.

    The grouping switch (AITEAM_TOOLSETS) + the read-only tier (AITEAM_READONLY) take effect at this registration stage:
      - default, no env → all 20 sets, 142 tools in total, register (backward compatible);
      - AITEAM_TOOLSETS selecting sets → only the modules hitting the set names register;
      - AITEAM_READONLY=1 → after registration, write tools are removed per WRITE_TOOLS, keeping only read tools.
    Unregistered tools are naturally uncallable — a double safeguard.
    """
    enabled = resolve_toolsets(os.environ.get("AITEAM_TOOLSETS"))
    for module in _MODULES:
        shortname = module.__name__.rsplit(".", 1)[-1]
        if module_enabled(shortname, enabled):
            module.register(mcp)

    if resolve_readonly(os.environ.get("AITEAM_READONLY")):
        removed = _remove_write_tools(mcp)
        if removed:
            logger.info("AITEAM_READONLY: removed %d write tools", len(removed))
