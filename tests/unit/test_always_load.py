"""Progressive tool loading P1 — alwaysLoad dynamic rotation unit tests.

Coverage over four layers:
1. Pure logic (compute_rotation / build_candidates): the cross-day threshold downstream, frequency sorting, the hard
cap,
   hysteresis anti-jitter (no swap at 1.1x / swap at 1.3x / an incumbent falling below the threshold is out), cold start
   with empty data.
2. Repository SQL (alwaysload_tool_frequencies): the cross-day threshold blocks single-day bursts, frequency descending,
a 7-day window, prefix filtering.
3. Endpoint (GET /api/tools/always-load): audit event writing, hysteresis baseline continuity, silently returning empty
on failure.
4. MCP server side mounting (apply_always_load_meta): the meta must really appear in tools/list's
   in `_meta` — the assertions cross the `to_mcp_tool()` serialization boundary instead of only looking at values
   assigned on in-memory objects.
   This also pins down the fastmcp behavior assumption that '``list_tools()`` returns live components rather than
   copies'.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.always_load import (
    ALWAYSLOAD_TARGET,
    ROTATION_EVENT_TYPE,
    Candidate,
    build_candidates,
    compute_rotation,
    normalize_tool_name,
    parse_registered_param,
)
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentActivityModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentActivity

# ============================================================
# Part A — Pure logic (no I/O)
# ============================================================


def test_normalize_strips_prefix():
    assert normalize_tool_name("mcp__ai-team-os__task_memo_add") == "task_memo_add"
    # No prefix: returned as is
    assert normalize_tool_name("Bash") == "Bash"


def test_parse_registered_param():
    assert parse_registered_param("") is None
    assert parse_registered_param("a, b ,c") == {"a", "b", "c"}
    assert parse_registered_param("  ,  ") is None


def test_build_candidates_normalize_and_registered_filter():
    rows = [
        ("mcp__ai-team-os__task_memo_add", 100, 5),
        ("mcp__ai-team-os__memory_search", 40, 3),
        ("mcp__ai-team-os__ghost_tool", 30, 2),  # A deleted tool, not in registered
    ]
    registered = {"task_memo_add", "memory_search"}
    cands = build_candidates(rows, registered)
    assert [c.name for c in cands] == ["task_memo_add", "memory_search"]
    # Frequency in descending order
    assert cands[0].count == 100 and cands[1].count == 40


def test_build_candidates_no_registration_filter():
    rows = [("mcp__ai-team-os__foo", 10, 2)]
    cands = build_candidates(rows, None)
    assert [c.name for c in cands] == ["foo"]


def test_compute_rotation_hard_cap_and_target():
    # 6 eligible candidates, target=3 → keep only the 3 with the highest frequencies
    cands = [Candidate(f"t{i}", count=100 - i, days=3) for i in range(6)]
    result = compute_rotation(cands, incumbents=[])
    assert len(result.tools) == ALWAYSLOAD_TARGET
    assert result.names == ["t0", "t1", "t2"]
    assert result.added == ["t0", "t1", "t2"]
    assert result.removed == []


def test_compute_rotation_cold_start_empty():
    result = compute_rotation([], incumbents=[])
    assert result.names == []
    assert result.added == []
    assert result.removed == []


def test_compute_rotation_data_insufficient_no_padding():
    # Only 2 eligible → return 2; do not pad to 3
    cands = [Candidate("a", 50, 3), Candidate("b", 40, 2)]
    result = compute_rotation(cands, incumbents=[])
    assert result.names == ["a", "b"]


def test_hysteresis_challenger_1_1x_no_swap():
    # A slot is filled by 3 incumbents; challenger d's frequency = the weakest incumbent ×1.1, short of 1.2x → no swap
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),  # The weakest incumbent
        Candidate("d", 110, 3),  # Challenger 110 = 100×1.1 < 100×1.2
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "c"}
    assert result.added == []
    assert result.removed == []


def test_hysteresis_challenger_1_3x_swap():
    # Challenger d's frequency = the weakest incumbent ×1.3 > 1.2x → displaces the weakest incumbent c
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),
        Candidate("d", 130, 3),
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "d"}
    assert result.added == ["d"]
    assert result.removed == ["c"]


def test_incumbent_drops_below_threshold_removed():
    # Incumbent c is not eligible this period (not among the candidates; blocked by the cross-day threshold) → out; the
    # empty slot is filled directly by challenger d
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("d", 50, 3),  # A challenger with a low frequency but an empty slot to enter directly
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "d"}
    assert result.added == ["d"]
    assert result.removed == ["c"]


def test_incumbent_full_slots_weak_challenger_stays_out():
    # All incumbents are still eligible and fill the slots; the challenger cannot displace any → the roster is unchanged
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),
        Candidate("d", 90, 3),
        Candidate("e", 80, 3),
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "c"}
    assert result.added == [] and result.removed == []


# ============================================================
# Part B — Repository SQL
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _insert_activity(
    repo: StorageRepository,
    tool_name: str,
    ts: datetime,
    agent_id: str = "agent-1",
) -> None:
    """Insert one agent_activities row directly, with a specified timestamp (create_activity does not support custom
    times)."""
    activity = AgentActivity(
        agent_id=agent_id,
        session_id="sess-1",
        tool_name=tool_name,
        timestamp=ts,
    )
    orm = AgentActivityModel.from_pydantic(activity)
    async with get_session(repo._db_url) as session:
        session.add(orm)


async def test_sql_cross_day_threshold_blocks_single_day_burst(repo: StorageRepository):
    now = utc_now()
    # burst: 10 times on the same day → cross-day count=1, blocked
    for _ in range(10):
        await _insert_activity(repo, "mcp__ai-team-os__burst_tool", now)
    # spread: once on each of 2 days → cross-day count=2, selected
    await _insert_activity(repo, "mcp__ai-team-os__spread_tool", now)
    await _insert_activity(repo, "mcp__ai-team-os__spread_tool", now - timedelta(days=1))

    rows = await repo.alwaysload_tool_frequencies()
    names = [r[0] for r in rows]
    assert "mcp__ai-team-os__spread_tool" in names
    assert "mcp__ai-team-os__burst_tool" not in names


async def test_sql_frequency_desc_order(repo: StorageRepository):
    now = utc_now()
    yesterday = now - timedelta(days=1)
    # high: 4 times over 2 days in total
    for ts in (now, now, yesterday, yesterday):
        await _insert_activity(repo, "mcp__ai-team-os__high", ts)
    # low: 2 times over 2 days in total
    for ts in (now, yesterday):
        await _insert_activity(repo, "mcp__ai-team-os__low", ts)

    rows = await repo.alwaysload_tool_frequencies()
    assert rows[0][0] == "mcp__ai-team-os__high"
    assert rows[0][1] == 4
    assert rows[1][0] == "mcp__ai-team-os__low"


async def test_sql_seven_day_window_and_prefix_filter(repo: StorageRepository):
    now = utc_now()
    # Activity from 8 days ago → outside the window, excluded
    await _insert_activity(repo, "mcp__ai-team-os__stale", now - timedelta(days=8))
    await _insert_activity(repo, "mcp__ai-team-os__stale", now - timedelta(days=9))
    # A non-mcp prefix → excluded by the prefix filter (even if cross-day eligible)
    await _insert_activity(repo, "Bash", now)
    await _insert_activity(repo, "Bash", now - timedelta(days=1))

    rows = await repo.alwaysload_tool_frequencies()
    names = [r[0] for r in rows]
    assert "mcp__ai-team-os__stale" not in names
    assert "Bash" not in names


# ============================================================
# Part C — Endpoint
# ============================================================


@pytest.fixture()
def app_ctx():
    """A TestClient + repo with in-memory SQLite."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    client = TestClient(app)
    yield client, repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _seed(repo: StorageRepository, tool_name: str, count_today: int, count_yesterday: int) -> None:
    now = utc_now()
    yesterday = now - timedelta(days=1)
    loop = asyncio.get_event_loop()
    for _ in range(count_today):
        loop.run_until_complete(_insert_activity(repo, tool_name, now))
    for _ in range(count_yesterday):
        loop.run_until_complete(_insert_activity(repo, tool_name, yesterday))


def test_endpoint_cold_start_empty_and_audit_written(app_ctx):
    client, repo = app_ctx
    resp = client.get("/api/tools/always-load")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == []
    # A cold start also records one audit event row (which is also next period's hysteresis baseline)
    ev = client.get(f"/api/events?type={ROTATION_EVENT_TYPE}&limit=5")
    assert ev.status_code == 200
    assert ev.json()["total"] >= 1


def test_endpoint_computes_and_writes_named_result(app_ctx):
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__task_memo_add", 5, 5)
    _seed(repo, "mcp__ai-team-os__memory_search", 3, 3)
    resp = client.get(
        "/api/tools/always-load?registered=task_memo_add,memory_search"
    )
    body = resp.json()
    assert set(body["tools"]) == {"task_memo_add", "memory_search"}
    assert set(body["added"]) == {"task_memo_add", "memory_search"}

    # The names in the audit event's data.tools agree with the results
    ev = client.get(f"/api/events?type={ROTATION_EVENT_TYPE}&limit=1").json()
    tools_data = ev["data"][0]["data"]["tools"]
    assert {t["name"] for t in tools_data} == {"task_memo_add", "memory_search"}


def test_endpoint_hysteresis_baseline_continuity(app_ctx):
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 30, 30)
    _seed(repo, "mcp__ai-team-os__b", 20, 20)
    _seed(repo, "mcp__ai-team-os__c", 10, 10)
    reg = "registered=a,b,c"
    # First time: no baseline → all three are swapped in
    first = client.get(f"/api/tools/always-load?{reg}").json()
    assert set(first["tools"]) == {"a", "b", "c"}
    assert set(first["added"]) == {"a", "b", "c"}
    # Second time: read the previous event as the incumbents → no change (both swap-in and swap-out are empty)
    second = client.get(f"/api/tools/always-load?{reg}").json()
    assert set(second["tools"]) == {"a", "b", "c"}
    assert second["added"] == []
    assert second["removed"] == []


def test_endpoint_failure_returns_empty_silently(app_ctx, monkeypatch):
    client, repo = app_ctx

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(repo, "alwaysload_tool_frequencies", _boom)
    resp = client.get("/api/tools/always-load")
    assert resp.status_code == 200
    assert resp.json()["tools"] == []


# ============================================================
# Part D — MCP server-side mounting
# ============================================================


def _tiny_server():
    """A mini FastMCP with two tools, for verifying meta mounting."""
    from fastmcp import FastMCP

    server = FastMCP(name="alwaysload-test")

    @server.tool
    def winner(a: int) -> str:
        """Winner tool.

        Args:
            a: number
        """
        return "w"

    @server.tool
    def loser(a: int) -> str:
        """Loser tool.

        Args:
            a: number
        """
        return "l"

    return server


def _mcp_meta(server, tool_name: str) -> dict:
    """Take a tool's _meta after serialization via to_mcp_tool() — the copy the client really sees."""
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == tool_name)
    return tool.to_mcp_tool().meta or {}


def test_apply_meta_lands_in_serialized_tool(monkeypatch):
    """The mounted meta must survive the to_mcp_tool() serialization boundary, otherwise the CC side cannot see the
    exemption."""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: ["winner"])
    server = _tiny_server()

    tagged = _alwaysload.apply_always_load_meta(server)

    assert tagged == ["winner"]
    assert _mcp_meta(server, "winner").get(_alwaysload.ALWAYSLOAD_META_KEY) is True
    assert _alwaysload.ALWAYSLOAD_META_KEY not in _mcp_meta(server, "loser")


def test_apply_meta_passes_registered_names_to_api(monkeypatch):
    """The registered passed to the API must be the bare tool names actually on the roster."""
    from aiteam.mcp import _alwaysload

    seen: list[list[str]] = []

    def _capture(registered):
        seen.append(sorted(registered))
        return []

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", _capture)
    assert _alwaysload.apply_always_load_meta(_tiny_server()) == []
    assert seen == [["loser", "winner"]]


def test_apply_meta_preserves_existing_meta(monkeypatch):
    """An existing meta is merged, not overwritten."""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: ["winner"])
    server = _tiny_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "winner")
    tool.meta = {"keep": "me"}

    _alwaysload.apply_always_load_meta(server)

    meta = _mcp_meta(server, "winner")
    assert meta.get("keep") == "me"
    assert meta.get(_alwaysload.ALWAYSLOAD_META_KEY) is True


def test_apply_meta_noop_when_api_returns_nothing(monkeypatch):
    """The API returns an empty roster (this path is also taken when the service is down/times out) → no tool gets
    meta mounted; all are deferred."""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: [])
    server = _tiny_server()
    assert _alwaysload.apply_always_load_meta(server) == []
    assert _alwaysload.ALWAYSLOAD_META_KEY not in _mcp_meta(server, "winner")
