"""HTTP request-level ledger — giving the "zero calls" determination a second caliber.

Previously, judging "nobody uses a certain tool" had only **one** collection surface: the CC-side observation of MCP
tool calls. But MCP
tools internally all convert into HTTP calls to the local API, and the Dashboard, hooks, scripts, and other sessions all
go through the same
batch of endpoints — so "zero calls" actually only meant "this one surface did not see it", which could rule out neither
a broken collection chain nor
caliber isolation. With the request-level ledger in place, zero calls finally gains a second caliber for
cross-validation.

Three design constraints (none of them is laziness; all are this repo's existing red lines):

* **No new tables.** The I10 machine check requires the set of tables declared by the ORM to match the real database,
  and the production instance cannot be restarted just to create a table;
  events is already an append-only ledger, and aggregate rows naturally belong to it.
* **No new noise.** Persisting every request would drown the ledger (events on this machine already exceeds 50k rows),
  so counting is done in memory by
  "method × path template × source", aggregated **hourly** into a single rollup event. Hours were chosen because
  the time resolution of the zero-calls determination is inherently at the day/week level; hours already far exceed the
  need, and anything finer only grows row counts, not information.
* **No timers.** This repo deliberately has no background daemon/cron, so it uses **lazy rollover**: when the next
  request arrives and
  discovers the bucket has rolled over, the previous bucket is persisted. The cost is that the last bucket only lands
  when the next request comes — for the question "who is being
  called", this has no impact (the absence of requests is itself the answer).

Paths uniformly use **templates** (/api/tasks/{task_id}) rather than actual paths; otherwise ids would blow up the
cardinality.
"""

from __future__ import annotations

import logging
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from aiteam.clock import utc_now

logger = logging.getLogger(__name__)

ROLLUP_EVENT = "api.request_rollup"

# Paths that say nothing about who uses which capability.
_SKIP_PREFIXES = ("/assets", "/docs", "/openapi", "/favicon", "/ws")


def classify_source(headers, user_agent: str) -> str:
    """Coarse caller bucket — deliberately 4 values, not a fingerprint.

    The point is only to tell "nobody calls this" apart from "one surface calls
    it a lot", so anything finer would be noise.
    """
    explicit = ""
    try:
        explicit = (headers.get("x-aiteam-source") or "").strip()
    except Exception:  # noqa: BLE001
        explicit = ""
    if explicit:
        return explicit
    ua = user_agent or ""
    if ua.startswith("Mozilla") or "Chrome" in ua or "Safari" in ua:
        return "dashboard"
    if "urllib" in ua or "python" in ua.lower() or "httpx" in ua or "curl" in ua:
        # Hooks and MCP tools share urllib; separating them needs the explicit
        # header above, which callers can opt into.
        return "hook-or-mcp"
    return "unknown"


def hour_bucket(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y-%m-%dT%H")


class RequestLedger:
    """In-memory counters + lazy hourly rollover into the event stream."""

    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._bucket: str | None = None
        self._counts: dict[str, int] = {}

    def record(self, method: str, path_template: str, source: str, bucket: str) -> None:
        if self._bucket is None:
            self._bucket = bucket
        key = f"{method} {path_template}|{source}"
        self._counts[key] = self._counts.get(key, 0) + 1

    async def observe_bucket(self, bucket: str) -> None:
        """Called on every request; flushes the previous bucket when it rolls over."""
        if self._bucket is None or bucket == self._bucket:
            return
        await self._flush()
        self._bucket = bucket

    async def _flush(self) -> None:
        counts, bucket = self._counts, self._bucket
        # Clear first: a failing flush must not make the next one double-count.
        self._counts = {}
        if not counts:
            return
        endpoints = {key.split("|", 1)[0] for key in counts}
        try:
            await self._bus.emit(
                ROLLUP_EVENT,
                "api:request_ledger",
                {
                    "bucket": bucket,
                    "counts": counts,
                    "total": sum(counts.values()),
                    "distinct_endpoints": len(endpoints),
                },
            )
        except Exception:  # noqa: BLE001
            # Bookkeeping must never take down the request path it observes.
            logger.debug("request ledger flush failed", exc_info=True)


class _LazyBus:
    """Resolves the real EventBus at flush time.

    Middleware is wired at create_app(); the bus only exists after startup
    initialisation, so holding a reference here would capture ``None`` forever.
    """

    async def emit(self, event_type: str, source: str, data: dict) -> None:
        from aiteam.api.deps import get_event_bus

        await get_event_bus().emit(event_type, source, data)


class RequestLedgerMiddleware(BaseHTTPMiddleware):
    """Counts every API request by (method × route template × caller bucket)."""

    def __init__(self, app, ledger: RequestLedger | None = None) -> None:
        super().__init__(app)
        self._ledger = ledger or RequestLedger(_LazyBus())

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        response = await call_next(request)
        try:
            # The router fills scope["route"] during call_next, which is what
            # turns /api/tasks/<uuid> back into /api/tasks/{task_id}.
            route = request.scope.get("route")
            template = getattr(route, "path", None) or path
            source = classify_source(request.headers, request.headers.get("user-agent", ""))
            bucket = hour_bucket()
            self._ledger.record(request.method, template, source, bucket)
            await self._ledger.observe_bucket(bucket)
        except Exception:  # noqa: BLE001
            logger.debug("request ledger record failed", exc_info=True)
        return response
