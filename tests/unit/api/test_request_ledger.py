"""HTTP request-level ledger — closing the observability broken chain, batch (5).

Why it is needed: previously, judgments of 'a tool has zero calls' had only **one** basis — the MCP tool-call
observations on the CC side. But inside MCP tools everything is converted to HTTP calls against the local API, and the
Dashboard, hooks, scripts and other
sessions all go through the same batch of HTTP endpoints. So 'zero calls' actually only meant 'this one collection
surface did not see it',
which can rule out neither a broken collection chain nor a metric isolation — exactly the cause named by Council
discipline (1), 'no-data≠zero'.
Adding the request-level ledger gives zero-call judgments a second basis for cross-validation.

Design constraints:
* **No new tables**. The I10 machine check keeps the ORM-declared table set consistent with the real database, and
production instances cannot be restarted to create tables;
  and the event stream is itself an append-only ledger, so aggregate rows naturally belong to it.
* **No new noise**. Persisting per request would drown the ledger (this machine's events are already 50K+), so counting
is done in memory by
  'method × path template × source', **aggregated hourly** into a single rollup event.
* **No timers**. This repo deliberately has no background daemons/cron, so it uses **lazy rolling**: when the next
request comes in
  and is found to cross a bucket boundary, the previous bucket is persisted.
* Paths use **templates** (/api/tasks/{task_id}) rather than actual paths, otherwise ids would blow up the cardinality.
"""

from __future__ import annotations

import pytest

from aiteam.api.request_ledger import RequestLedger


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kw):
        self.events.append((event_type, source, data))

    def rollups(self) -> list[dict]:
        return [d for t, _s, d in self.events if t == "api.request_rollup"]


@pytest.mark.asyncio()
async def test_counts_are_aggregated_not_per_request():
    """30 similar requests within one hour → 1 rollup row persisted, not 30."""
    bus = _Bus()
    ledger = RequestLedger(bus)
    for _ in range(30):
        ledger.record("GET", "/api/tasks/{task_id}", "mcp", bucket="2026-07-28T15")
    # Crossing a bucket triggers the lazy roll
    await ledger.observe_bucket("2026-07-28T16")

    (rollup,) = bus.rollups()
    assert rollup["bucket"] == "2026-07-28T15"
    assert rollup["total"] == 30
    assert rollup["counts"]["GET /api/tasks/{task_id}|mcp"] == 30


@pytest.mark.asyncio()
async def test_dimensions_are_kept_separate():
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    ledger.record("POST", "/api/tasks", "mcp", bucket="b1")
    ledger.record("GET", "/api/tasks", "dashboard", bucket="b1")
    await ledger.observe_bucket("b2")

    (rollup,) = bus.rollups()
    assert rollup["counts"] == {
        "GET /api/tasks|mcp": 1,
        "POST /api/tasks|mcp": 1,
        "GET /api/tasks|dashboard": 1,
    }
    assert rollup["total"] == 3
    assert rollup["distinct_endpoints"] == 2


@pytest.mark.asyncio()
async def test_no_flush_while_inside_same_bucket():
    """Nothing is persisted within the same bucket — otherwise it becomes per-request writing again."""
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/health", "hook", bucket="b1")
    await ledger.observe_bucket("b1")
    assert bus.rollups() == []


@pytest.mark.asyncio()
async def test_empty_bucket_writes_nothing():
    """With no requests, no empty ledger page should be left behind."""
    bus = _Bus()
    ledger = RequestLedger(bus)
    await ledger.observe_bucket("b2")
    assert bus.rollups() == []


@pytest.mark.asyncio()
async def test_flush_is_idempotent():
    """After rolling, the old bucket is cleared; repeated rolling does not double-count."""
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    await ledger.observe_bucket("b2")
    await ledger.observe_bucket("b3")
    assert len(bus.rollups()) == 1


@pytest.mark.asyncio()
async def test_emit_failure_never_breaks_the_request_path():
    """A failure to persist the ledger must never take down the request being served."""

    class _Broken:
        async def emit(self, *_a, **_kw):
            raise RuntimeError("bus down")

    ledger = RequestLedger(_Broken())
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    await ledger.observe_bucket("b2")  # Does not raise


def test_source_classification():
    """Source bucketing: MCP tools/hooks all go through urllib, browsers through the UA; when unsure, unknown."""
    from aiteam.api.request_ledger import classify_source

    assert classify_source({"x-aiteam-source": "mcp"}, "") == "mcp"
    assert classify_source({}, "Mozilla/5.0 (Macintosh)") == "dashboard"
    assert classify_source({}, "Python-urllib/3.12") == "hook-or-mcp"
    assert classify_source({}, "") == "unknown"
