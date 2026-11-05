"""At session start, attach the alwaysLoad meta to hit tools (tool progressive loading P1, MCP server side).

After register_all(mcp) has registered all tools, it calls ``apply_always_load_meta(mcp)``: query the local API's
GET /api/tools/always-load for the recent high-frequency whitelist, and attach to the corresponding registered tool
components
``meta["anthropic/alwaysLoad"] = True``. FastMCP's ``Tool.to_mcp_tool()`` serializes the component's ``meta`` via
``get_meta()``
into the ``_meta`` field of tools/list, and CC exempts them from defer accordingly.

Best-effort throughout: API absent / timeout / parse failure are all silent, and every tool still goes through
ToolSearch as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request

from aiteam.mcp._base import _get_api_url

logger = logging.getLogger(__name__)

# The resident exemption key CC recognizes; a True value means direct access with no search.
ALWAYSLOAD_META_KEY = "anthropic/alwaysLoad"
# Never wait long on the startup path — if it is not obtained within 2 seconds, treat it as absent, all defer.
_TIMEOUT_S = 2.0


def _tool_components(mcp) -> list[object]:
    """The list of currently registered tool components; any failure returns an empty list.

    Goes through the public ``mcp.list_tools()``. It is a coroutine, but this function is only called on the stdio
    startup path
    (before ``mcp.run()``, with no running event loop yet), so ``asyncio.run``
    is safe; if it is ever called inside an existing event loop, it silently degrades to empty (all defer).

    Two premises verified in practice (true for fastmcp 3.4.3 / 3.4.5; must be re-verified when upgrading to 4.0):
      * ``list_tools()`` returns the component objects themselves, not copies, so assigning to their ``meta`` in place
        reaches ``tools/list``'s ``_meta`` via ``to_mcp_tool()``;
      * the return value contains only tools, no resources/prompts, so no type filtering is needed.

    The old implementation read the private component table ``mcp.local_provider._components`` and imported Tool from
    ``fastmcp.tools.base`` for type filtering — both are certain breakage points when fastmcp 4.0
    removes the 3.x compatibility shims; they have been replaced with the public surface.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # No running event loop, so asyncio.run is fine
    else:
        logger.debug("alwaysLoad: already inside an event loop, skipping tool enumeration")
        return []
    try:
        return list(asyncio.run(mcp.list_tools()))
    except Exception:
        logger.debug("alwaysLoad: tool enumeration failed", exc_info=True)
        return []


def _registered_tool_names(mcp) -> list[str]:
    """The list of bare tool names actually registered."""
    names: list[str] = []
    for comp in _tool_components(mcp):
        name = getattr(comp, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _fetch_always_load(registered: list[str]) -> list[str]:
    """Call GET /api/tools/always-load and return the bare tool name list; any failure returns an empty list."""
    try:
        query = urllib.parse.urlencode({"registered": ",".join(registered)})
        url = f"{_get_api_url()}/api/tools/always-load?{query}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tools = payload.get("tools", [])
        return [t for t in tools if isinstance(t, str)]
    except Exception:
        return []


def apply_always_load_meta(mcp) -> list[str]:
    """Attach the alwaysLoad meta to the hit registered tools. Fails silently; returns the list of tool names
    actually attached."""
    try:
        components = _tool_components(mcp)
        if not components:
            return []
        registered = [
            n for n in (getattr(c, "name", None) for c in components) if isinstance(n, str) and n
        ]
        winners = set(_fetch_always_load(registered))
        if not winners:
            return []

        tagged: list[str] = []
        for comp in components:
            name = getattr(comp, "name", None)
            if name in winners:
                existing = getattr(comp, "meta", None)
                meta = dict(existing) if existing else {}
                meta[ALWAYSLOAD_META_KEY] = True
                comp.meta = meta  # type: ignore[attr-defined]
                tagged.append(name)
        if tagged:
            logger.info("alwaysLoad meta applied to %d tools: %s", len(tagged), tagged)
        return tagged
    except Exception:
        logger.debug("alwaysLoad meta application skipped", exc_info=True)
        return []
