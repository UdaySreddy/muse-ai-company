"""AI Team OS — Tool progressive-loading P1 routes.

GET /api/tools/always-load: called by the OS's MCP server during session startup; recomputes the recently high-frequency
MCP tools' alwaysLoad whitelist (one SQL query + hysteresis debounce) and returns a list of bare tool names. The MCP
server
uses it to attach ``_meta {"anthropic/alwaysLoad": true}`` to matched tools, exempting them from defer.

See the P1 section of docs/tool-loading-design.md for the design spec. The feature is a pure gain: if any step fails, it
returns
an empty list with 200; everything proceeds via ToolSearch as before and never throws a 5xx.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from aiteam.api.always_load import (
    ROTATION_EVENT_TYPE,
    build_candidates,
    compute_rotation,
    parse_registered_param,
)
from aiteam.api.deps import get_scoped_repository
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("/always-load")
async def get_always_load(
    registered: str = Query(
        "",
        description="Bare tool names actually registered by the current MCP server (comma-separated); used to filter "
                    "out deleted tools."
        "Leave empty to skip registration filtering (e.g. for manual debugging).",
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Recompute the alwaysLoad rotation list and record an audit event.

    - Data: ``mcp__ai-team-os%`` tools in agent_activities over the last 7 days, spanning ≥2 days, in descending
      frequency order;
    - Normalization: strip the prefix to get bare names and filter by ``registered`` to the tools currently registered;
    - Hysteresis: read the most recent rotation event's ``data.tools`` as incumbents; a challenger must exceed
      incumbent×1.2 to rotate in;
    - Hard cap 5, target 3; do not pad when data is insufficient;
    - The computed result is recorded as one events audit row (also the next period's hysteresis baseline);
    - If any step fails → return an empty list with 200.
    """
    try:
        rows = await repo.alwaysload_tool_frequencies()
        registered_set = parse_registered_param(registered)
        candidates = build_candidates(rows, registered_set)

        prev = await repo.list_events(event_type=ROTATION_EVENT_TYPE, limit=1)
        incumbents: list[str] = []
        if prev:
            tools_data = prev[0].data.get("tools") or []
            incumbents = [
                t["name"]
                for t in tools_data
                if isinstance(t, dict) and isinstance(t.get("name"), str)
            ]

        result = compute_rotation(candidates, incumbents)

        # Record the computed result as one ledger row — audit and next period's hysteresis baseline combined; no new
        # table and no config writes.
        await repo.create_event(
            event_type=ROTATION_EVENT_TYPE,
            source="api.tools.always_load",
            data={
                "tools": [{"name": c.name, "count": c.count} for c in result.tools],
                "added": result.added,
                "removed": result.removed,
            },
        )

        return {
            "tools": result.names,
            "detail": [{"name": c.name, "count": c.count, "days": c.days} for c in result.tools],
            "added": result.added,
            "removed": result.removed,
        }
    except Exception:  # noqa: BLE001 — Pure-gain feature; if it breaks there is no loss — silently degrade to deferring everything.
        logger.exception("alwaysLoad rotation failed; returning empty whitelist")
        return {"tools": [], "detail": [], "added": [], "removed": []}
