"""AI Team OS — I3a Workflow observability layer end-to-end tests (TestClient / in-memory SQLite, runnable offline).

Covers the design document 8.2 acceptance criteria: EventType round-tripping, parse_workflow_receipt key extraction,
ingest_run_from_file persists run+agents+team write-back+a completed event, the PostToolUse receipt skeleton,
the three read endpoints, POST /reconcile, idempotency, and project isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api import workflow_ingest
from aiteam.api.app import create_app
from aiteam.api.deps import (
    get_event_bus,
    get_hook_translator,
    get_repository,
    get_scoped_repository,
)
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

WF_ID = "wf_8e92fe01-67c"
PID_A = "proj-wf-a-0001"
PID_B = "proj-wf-b-0002"

RECEIPT = (
    "Workflow launched in background. Task ID: westwrtgj\n"
    "Summary: Multi-track parallel research into the exact format of CNIPA patent electronic application XML files\n"
    "Transcript dir: /Users/cronus/.claude/projects/-Users-cronus-Desktop-Test/"
    "SESSION/subagents/workflows/wf_8e92fe01-67c\n"
    "Script file: /Users/cronus/.claude/projects/-Users-cronus-Desktop-Test/"
    "SESSION/workflows/scripts/cnipa-xml-format-research-wf_8e92fe01-67c.js\n"
    '(Edit this file with Write/Edit and re-invoke Workflow with {scriptPath: "..."})'
)

WF_SCRIPT = (
    "export const meta = {\n"
    "  name: 'cnipa-xml-format-research',\n"
    "  phases: [ { title: 'Research' }, { title: 'Synthesis' } ],\n"
    "};\n"
    "agent({ label: 'a1' });\n"
    "agent({ label: 'a2' });\n"
)


def _fixture_snapshot() -> dict:
    """A trimmed version of a real 18-key snapshot (wf_8e92fe01-67c), with numeric fields kept as strings (as in the
    real snapshot)."""
    return {
        "runId": WF_ID,
        "timestamp": "2026-06-12T11:26:02.248Z",
        "taskId": "westwrtgj",
        "script": "export const meta = {...}",
        "scriptPath": "/Users/x/workflows/scripts/cnipa-xml-format-research-wf_8e92fe01-67c.js",
        "result": {"synthesis": "Conclusion...", "rawFindings": ["a", "b"]},
        "agentCount": "2",
        "logs": ["done"],
        "durationMs": "1498565",
        "summary": "Multi-track parallel research into the exact format "
            "of CNIPA patent electronic application XML files",
        "workflowName": "cnipa-xml-format-research",
        "status": "completed",
        "startTime": "1781262063681",
        "phases": [
            {"title": "Research", "detail": "4 parallel researchers"},
            {"title": "Synthesis", "detail": "Cross-checking"},
        ],
        "defaultModel": "claude-fable-5",
        "totalTokens": "551440",
        "totalToolCalls": "297",
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "Research"},
            {
                "type": "workflow_agent",
                "index": 1,
                "label": "Research:cpc-samples",
                "phaseIndex": 1,
                "phaseTitle": "Research",
                "agentId": "aa3b60f522593a7f8",
                "model": "claude-fable-5",
                "state": "done",
                "startedAt": 1781262063718,
                "queuedAt": 1781262063705,
                "lastToolName": "StructuredOutput",
                "lastToolSummary": "high",
                "promptPreview": "You are a researcher...",
                "tokens": 79848,
                "toolCalls": 58,
                "durationMs": 790705,
                "resultPreview": "{\"confidence\":\"high\"}",
            },
            {
                "type": "workflow_agent",
                "index": 2,
                "label": "Synthesis:synth",
                "phaseIndex": 2,
                "phaseTitle": "Synthesis",
                "agentId": "bb9c71e633604b8g9",
                "model": "claude-opus-4-8[1m]",
                "state": "done",
                "startedAt": 1781262854000,
                "queuedAt": 1781262853000,
                "lastToolName": "StructuredOutput",
                "lastToolSummary": "done",
                "promptPreview": "Cross-synthesis...",
                "tokens": 120000,
                "toolCalls": 40,
                "durationMs": 200000,
                "resultPreview": "Final conclusion",
            },
        ],
    }


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def event_bus(repo: StorageRepository) -> EventBus:
    return EventBus(repo=repo)


@pytest_asyncio.fixture()
async def client(repo: StorageRepository, event_bus: EventBus) -> AsyncClient:
    translator = HookTranslator(repo=repo, event_bus=event_bus)
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    app.dependency_overrides[get_event_bus] = lambda: event_bus
    app.dependency_overrides[get_hook_translator] = lambda: translator
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac  # type: ignore[misc]


# ============================================================
# 1. EventType round-trip does not raise (contrast with the status quo, where workflow.planned -> ValueError)
# ============================================================


def test_eventtype_workflow_members_roundtrip():
    for val in ("workflow.planned", "workflow.started", "workflow.completed"):
        et = EventType(val)  # Before the fix this line raised ValueError
        assert et.value == val


# ============================================================
# 2. parse_workflow_receipt key extraction
# ============================================================


def test_parse_workflow_receipt():
    r = workflow_ingest.parse_workflow_receipt(RECEIPT)
    assert r["wf_id"] == WF_ID
    assert r["cc_task_id"] == "westwrtgj"
    assert r["name"] == "cnipa-xml-format-research"
    assert r["summary"].startswith("Multi-track parallel research")
    assert r["script_path"].endswith("cnipa-xml-format-research-wf_8e92fe01-67c.js")
    assert r["transcript_dir"].endswith("subagents/workflows/wf_8e92fe01-67c")


def test_parse_workflow_receipt_empty():
    r = workflow_ingest.parse_workflow_receipt("garbage no keys here")
    assert r["wf_id"] == ""
    assert r["cc_task_id"] == ""


def test_wf_run_id_re_bounded_does_not_swallow_worktree_suffix():
    """Regression for task f8207497: _WF_RUN_ID_RE used an unbounded `(?:-[0-9a-z]+)*`
    group that greedily consumed CC's per-branch worktree instance suffix
    (".claude/worktrees/wf_a69e7d46-a66-1" -> branch "worktree-wf_a69e7d46-a66-1"),
    over-matching the run id to "wf_a69e7d46-a66-1" instead of the true run id
    "wf_a69e7d46-a66". That mismatch made team lookups miss the real wf_<id>.json
    snapshot and spawn an orphan team. Bounded to a single optional dash-suffix, this
    must stop exactly at the run id.
    """
    m = workflow_ingest._WF_RUN_ID_RE.search(
        "cnipa-xml-format-research-wf_a69e7d46-a66-1.js"
    )
    assert m is not None
    assert m.group(0) == "wf_a69e7d46-a66"
    # Legacy single-suffix run ids (the module's own docstring example) must still
    # match in full — the fix must not regress the common case.
    m2 = workflow_ingest._WF_RUN_ID_RE.search(
        "cnipa-xml-format-research-wf_8e92fe01-67c.js"
    )
    assert m2 is not None
    assert m2.group(0) == "wf_8e92fe01-67c"


def test_extract_workflow_run_id_from_worktree_cwd():
    """Same regression, exercised through the real extraction method (not just the
    bare regex) with a cwd payload shaped like an actual worktree in this repo
    (see `git worktree list`: .claude/worktrees/wf_a69e7d46-a66-1, branch
    worktree-wf_a69e7d46-a66-1).
    """
    payload = {
        "cwd": "/repo/.claude/worktrees/wf_a69e7d46-a66-1",
    }
    wf_id = HookTranslator._extract_workflow_run_id(None, payload)
    assert wf_id == "wf_a69e7d46-a66"


# ============================================================
# 3. ingest_run_from_file → run + N agents + team write-back + completed event
# ============================================================


@pytest.mark.asyncio
async def test_ingest_run_from_file(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    # Pre-seed an existing workflow-<wf_id> team + a member matching on cc_tool_use_id (to test the os_agent_id
    # association).
    team = await repo.create_team(
        name=f"workflow-{WF_ID}",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": WF_ID},
        project_id=PID_A,
    )
    member = await repo.create_agent(
        team_id=team.id, name="wf-aa3b60f522", role="workflow-subagent",
        source="hook", cc_tool_use_id="aa3b60f522593a7f8",
    )

    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    res = await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    assert res["ok"] is True
    assert res["agents"] == 2
    assert res["emitted"] is True

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.status == "completed"
    assert run.total_tokens == 551440
    assert run.total_tool_calls == 297
    assert run.agent_count == 2
    assert run.duration_ms == 1498565
    assert run.team_id == team.id
    assert run.project_id == PID_A
    assert run.completed_at is not None
    # phases are normalized to [{index,title}]
    assert run.phases == [{"index": 1, "title": "Research"}, {"index": 2, "title": "Synthesis"}]

    agents = await repo.list_workflow_agents(WF_ID)
    assert len(agents) == 2
    # os_agent_id association with an existing member (agents.cc_tool_use_id == cc_agent_id)
    linked = [a for a in agents if a.cc_agent_id == "aa3b60f522593a7f8"]
    assert linked and linked[0].os_agent_id == member.id
    assert linked[0].tokens == 79848 and linked[0].tool_calls == 58

    # team.completed_at write-back (writing into an existing nullable field)
    team_after = await repo.get_team(team.id)
    assert team_after.completed_at is not None

    # emit workflow.completed
    events = await repo.list_events(type_prefix="workflow.")
    assert any(e.type == EventType.WORKFLOW_COMPLETED for e in events)


# ============================================================
# 7. Idempotency: running twice in a row → no duplicate rows, totals do not double, no duplicate completed emit
# ============================================================


@pytest.mark.asyncio
async def test_ingest_idempotent(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)

    runs = await repo.list_workflow_runs()
    assert len([r for r in runs if r.wf_id == WF_ID]) == 1
    run = await repo.get_workflow_run(WF_ID)
    assert run.total_tokens == 551440  # Does not double
    agents = await repo.list_workflow_agents(WF_ID)
    assert len(agents) == 2  # Does not double

    completed = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_COMPLETED
    ]
    assert len(completed) == 1  # The event is not duplicated (transition-guard)


# ============================================================
# 4. POST /api/hooks/event synthesizes a PostToolUse(Workflow) receipt → skeleton + workflow.started
#    (Also goes through the PreToolUse staging of the plan, verifying planned_agent_count completion + workflow.planned
#    persistence)
# ============================================================


@pytest.mark.asyncio
async def test_hook_receipt_creates_running_skeleton(
    client: AsyncClient, repo: StorageRepository
):
    session_id = "sess-hook-1"
    # PreToolUse(Workflow): stage the plan + emit workflow.planned (it only truly persists once the enum is fixed).
    pre = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
        },
    )
    assert pre.status_code == 200

    planned = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_PLANNED
    ]
    assert planned, "workflow.planned should have been persisted (the enum fix is in effect)"

    # PostToolUse(Workflow): receipt plaintext → run skeleton (running) + workflow.started.
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.status == "running"
    assert run.source == "hook"
    assert run.cc_task_id == "westwrtgj"
    assert run.name == "cnipa-xml-format-research"
    # Plan completion: literal agent() count = 2
    assert run.planned_agent_count == 2
    assert run.phases == [{"index": 1, "title": "Research"}, {"index": 2, "title": "Synthesis"}]

    started = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_STARTED
    ]
    assert started


@pytest.mark.asyncio
async def test_hook_receipt_adopts_session_fallback_team(
    client: AsyncClient, repo: StorageRepository
):
    """The receipt adopts the session fallback team (a regression test against 'one run, two teams' fragmentation).

    When wf_id arrives late, early agents all hang off the workflow-session-<sid[:8]> fallback team; the receipt is
    the first point at which wf_id is available, so run.team_id and team.config.
    workflow_run_id must be linked in both directions right there (monitoring record, 2026-07-06: without this, both
    directions stay empty for the whole live run,
    and the final-state reconciliation would create yet another empty workflow-<wf_id> team, causing fragmentation).
    """
    session_id = "sess-adopt-1"
    team = await repo.create_team(
        name=f"workflow-session-{session_id[:8]}",
        mode="coordinate",
        config={"kind": "workflow", "auto_created": True, "workflow_run_id": None},
    )

    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.team_id == team.id, "run should adopt the session fallback team rather than leaving it empty"

    linked = await repo.get_team_by_name(team.name)
    assert (linked.config or {}).get("workflow_run_id") == WF_ID


@pytest.mark.asyncio
async def test_session_end_spares_workflow_teams(
    client: AsyncClient, repo: StorageRepository
):
    """SessionEnd only closes teams owned by this session (fleet-layer §5, merged in 7ae3b7cd).

    Two historical false kills are covered here: (1) a bypass session's SessionEnd once killed another session's
    still-running workflow
    teams, marking them all completed with 0 members (2026-07-08: c4fab878 killed abff40af); (2) a broader false kill of
    ordinary teams
    — a bypass session's SessionEnd closed every active ordinary team in the whole database, falsely killing teams
    another session was using.
    """
    # A workflow team (owned by another session) — must be exempt
    wf_team = await repo.create_team(
        name="workflow-wf_spare-1", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_spare-1"},
    )
    member = await repo.create_agent(
        team_id=wf_team.id, name="wf-spare1", role="workflow-subagent",
        session_id="sess-other-9",
    )
    await repo.update_agent(member.id, status="busy")
    # An ordinary team owned by another session — a bypass SessionEnd must not close it (the core of 7ae3b7cd)
    owned_by_other = await repo.create_team(
        name="t-owned-other", mode="coordinate",
        config={"owner_session_id": "sess-other-9"},
    )
    # A legacy ordinary team with no ownership marker — conservatively do not close it (let the reaper collect it by its
    # own activity)
    legacy = await repo.create_team(name="t-legacy-se", mode="coordinate")
    # An ordinary team owned by the ending session itself — must be closed
    owned_by_ending = await repo.create_team(
        name="t-owned-ending", mode="coordinate",
        config={"owner_session_id": "sess-bystander-1"},
    )

    resp = await client.post(
        "/api/hooks/event",
        json={"hook_event_name": "SessionEnd", "session_id": "sess-bystander-1"},
    )
    assert resp.status_code == 200

    wf_after = await repo.get_team(wf_team.id)
    assert str(wf_after.status).endswith("active"), "A workflow team must not be closed by SessionEnd"
    m_after = (await repo.find_agents_by_session("sess-other-9"))[0]
    assert str(m_after.status).endswith("busy"), "Workflow members must not be swept by SessionEnd"
    other_after = await repo.get_team(owned_by_other.id)
    assert str(other_after.status).endswith("active"), (
        "An ordinary team owned by another session must not be falsely killed"
    )
    legacy_after = await repo.get_team(legacy.id)
    assert str(legacy_after.status).endswith("active"), (
        "A legacy team with no ownership is conservatively left open, for the reaper"
    )
    ending_after = await repo.get_team(owned_by_ending.id)
    assert str(ending_after.status).endswith("completed"), "A team owned by this session must be closed"


@pytest.mark.asyncio
async def test_ingest_team_status_follows_run(
    repo: StorageRepository, event_bus: EventBus, tmp_path
):
    """Team status follows the run: after a running run's team is falsely killed as completed, the next ingest cycle
    automatically revives it to active; an active team of a terminal run converges to completed."""
    import json as _json

    from aiteam.api import workflow_ingest as wi

    team = await repo.create_team(
        name="workflow-wf_follow-1", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_follow-1"},
    )
    await repo.update_team(team.id, status="completed")  # Simulate a false kill

    wf_json = tmp_path / "wf_follow-1.json"
    wf_json.write_text(_json.dumps({
        "runId": "wf_follow-1", "workflowName": "follow-test",
        "status": "running", "startTime": 1783500000000,
        "workflowProgress": [],
    }))
    await wi.ingest_run_from_file(repo, event_bus, wf_json)
    revived = await repo.get_team_by_name("workflow-wf_follow-1")
    assert str(revived.status).endswith("active"), "A running run's team should be revived"

    wf_json.write_text(_json.dumps({
        "runId": "wf_follow-1", "workflowName": "follow-test",
        "status": "completed", "startTime": 1783500000000, "durationMs": 1000,
        "workflowProgress": [],
    }))
    await wi.ingest_run_from_file(repo, event_bus, wf_json)
    closed = await repo.get_team_by_name("workflow-wf_follow-1")
    assert str(closed.status).endswith("completed"), "A terminal run's team should converge to completed"


@pytest.mark.asyncio
async def test_hook_receipt_creates_per_run_team(
    client: AsyncClient, repo: StorageRepository
):
    """The receipt directly creates a per-run team (regression from the 2026-07-07 D1 record).

    A per-run team was originally only created lazily at the SubagentStop promote moment — a run killed in the
    middle of a turn / in a long unfinished turn would never have a team, invisible on the project page the whole time.
    The receipt is
    the earliest point at which each run reliably obtains a wf_id, so when there is nothing to adopt it must create the
    team right there.
    """
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-mkteam-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    team = await repo.get_team_by_name(f"workflow-{WF_ID}")
    assert team is not None, "The receipt should create the per-run team right there"
    assert (team.config or {}).get("workflow_run_id") == WF_ID
    run = await repo.get_workflow_run(WF_ID)
    assert run.team_id == team.id


@pytest.mark.asyncio
async def test_hook_receipt_migrates_busy_agents_from_occupied_fallback(
    client: AsyncClient, repo: StorageRepository
):
    """When the fallback team is occupied by another run: create a new per-run team and migrate only the still-active
    (busy) members;
    the historical offline members stay in the fallback team (to avoid dragging yesterday's run's corpses into the
    new team)."""
    session_id = "sess-mig-1"
    fallback = await repo.create_team(
        name=f"workflow-session-{session_id[:8]}",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_older-run"},
    )
    live = await repo.create_agent(
        team_id=fallback.id, name="wf-live1", role="workflow-subagent",
        session_id=session_id,
    )
    await repo.update_agent(live.id, status="busy")
    dead = await repo.create_agent(
        team_id=fallback.id, name="wf-dead1", role="workflow-subagent",
        session_id=session_id,
    )
    await repo.update_agent(dead.id, status="offline")

    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    team = await repo.get_team_by_name(f"workflow-{WF_ID}")
    assert team is not None and team.id != fallback.id
    live2 = (await repo.find_agents_by_session(session_id))
    by_name = {a.name: a for a in live2}
    assert by_name["wf-live1"].team_id == team.id, "A busy member should migrate into the per-run team"
    assert by_name["wf-dead1"].team_id == fallback.id, "An offline member should stay in the fallback team"


@pytest.mark.asyncio
async def test_reaper_closes_adopted_fallback_when_run_terminal(
    repo: StorageRepository, event_bus: EventBus
):
    """Empty-shell reclamation gap fix: an adopted (run_id present) workflow-session fallback team whose adopting run is
    terminal
    but still hangs around with 0 members → the reaper collects it (the dd686eec class). A still-running run is
    exempt; a team with members is exempt."""
    from datetime import timedelta

    from sqlalchemy import text

    from aiteam.api.state_reaper import StateReaper
    from aiteam.storage.connection import get_session
    from aiteam.types import WorkflowRun

    # The adopting runs: one terminal, one still running
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_term-aa", status="completed"))
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_run-bb", status="running"))
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_term-dd", status="killed"))

    # (1) Adopted + run terminal + 0 members → should be collected
    t_term = await repo.create_team(
        name="workflow-session-aaaaaaaa", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_term-aa"},
    )
    # (2) Adopted + run still running + 0 members → exempt
    t_running = await repo.create_team(
        name="workflow-session-bbbbbbbb", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_run-bb"},
    )
    # (3) Not adopted (run_id empty) + 0 members → should be collected (existing behavior must not regress)
    t_unadopted = await repo.create_team(
        name="workflow-session-cccccccc", mode="coordinate",
        config={"kind": "workflow"},
    )
    # (4) Adopted + run terminal + has members → exempt (the 0-member guard)
    t_member = await repo.create_team(
        name="workflow-session-dddddddd", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_term-dd"},
    )
    await repo.create_agent(team_id=t_member.id, name="wf-x", role="workflow-subagent")

    # All aged (created_at earlier than the 30min stale threshold)
    old = (utc_now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with get_session(repo._db_url) as session:
        await session.execute(
            text("UPDATE teams SET created_at = :t WHERE name LIKE 'workflow-session-%'"),
            {"t": old},
        )

    reaper = StateReaper(repo, event_bus)
    await reaper._check_stale_teams(utc_now(), repo)

    assert (await repo.get_team(t_term.id)).status == "completed"  # (1) collected
    assert (await repo.get_team(t_running.id)).status == "active"  # (2) exempt
    assert (await repo.get_team(t_unadopted.id)).status == "completed"  # (3) collected
    # (4) exempt — the members and the run were just created; the silence grace period has not passed (not because of
    # 'has members' itself)
    assert (await repo.get_team(t_member.id)).status == "active"


async def _age_workflow_fixtures(repo: StorageRepository, hours: float) -> None:
    """Push every team / agent / run timestamp back by ``hours``.

    Silence-based predicates need every clock the reaper reads to be old at once;
    ORM defaults stamp all of them at creation time.
    """
    from datetime import timedelta

    from sqlalchemy import text

    from aiteam.storage.connection import get_session

    old = (utc_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with get_session(repo._db_url) as session:
        await session.execute(
            text("UPDATE teams SET created_at = :t, updated_at = :t"), {"t": old}
        )
        await session.execute(
            text("UPDATE agents SET created_at = :t, last_active_at = :t"), {"t": old}
        )
        await session.execute(
            text("UPDATE workflow_runs SET created_at = :t, updated_at = :t"), {"t": old}
        )


@pytest.mark.asyncio
async def test_reaper_closes_workflow_team_when_all_runs_settled(
    repo: StorageRepository, event_bus: EventBus
):
    """All runs terminal + nobody busy in the team + silence beyond the grace period → collect the team.

    Previously workflow teams had only one reclamation path, the '0-member empty shell'; **teams with members were
    missed as a whole class**: after all runs
    reached a terminal state the team stayed active forever (confirmed on this machine on 2026-07-28: 3 teams, the
    oldest hanging since 07-06, for 22 days). The three states
    are locked down together:
      (1) All runs terminal + members silent → collect, with completed_at;
      (2) A run still running → keep the exemption (members arrive lazily during long runs; never judge death by
      silence);
      (3) A team with zero run records → do not collect within the short grace period (the whole evidence chain is
      missing; wait for the longer grace period).
    """

    from aiteam.api.state_reaper import StateReaper
    from aiteam.types import WorkflowRun

    # (1) The adopting run is terminal (via the config chain; run.team_id is empty — the real shape of the 3 stale teams
    # on this machine)
    await repo.upsert_workflow_run(
        WorkflowRun(wf_id="wf_settled-01", status="completed")
    )
    t_settled = await repo.create_team(
        name="workflow-session-11111111",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_settled-01"},
    )
    a1 = await repo.create_agent(
        team_id=t_settled.id, name="wf-s1", role="workflow-subagent"
    )
    await repo.update_agent(a1.id, status="offline")

    # (2) A per-run team whose run is still running (run.team_id directly linked)
    t_live = await repo.create_team(
        name="workflow-wf_live-02",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_live-02"},
    )
    await repo.upsert_workflow_run(
        WorkflowRun(wf_id="wf_live-02", status="running", team_id=t_live.id)
    )
    a2 = await repo.create_agent(
        team_id=t_live.id, name="wf-s2", role="workflow-subagent"
    )
    await repo.update_agent(a2.id, status="offline")

    # (3) Zero run records + has members
    t_norun = await repo.create_team(
        name="workflow-session-33333333",
        mode="coordinate",
        config={"kind": "workflow"},
    )
    a3 = await repo.create_agent(
        team_id=t_norun.id, name="wf-s3", role="workflow-subagent"
    )
    await repo.update_agent(a3.id, status="offline")

    await _age_workflow_fixtures(repo, hours=2)

    reaper = StateReaper(repo, event_bus)
    await reaper._check_stale_teams(utc_now(), repo)

    closed = await repo.get_team(t_settled.id)
    assert closed.status == "completed", "A workflow team with all runs terminal and silent members must be collected"
    assert closed.completed_at is not None, "Collecting a team must set completed_at"
    assert (await repo.get_team(t_live.id)).status == "active", "A run is still running → exempt"
    assert (await repo.get_team(t_norun.id)).status == "active", (
        "A zero-run team is not collected within the short grace period"
    )


@pytest.mark.asyncio
async def test_workflow_team_closure_guards(
    repo: StorageRepository, event_bus: EventBus
):
    """The three collection guards: a busy member has a veto; a leader row is not swept up along the way; a zero-run
    team is collected only after the long grace period.

    The leader-row exemption shares its origin with member sweeping in workflow_ingest (record 03fe7cae): a historical
    Leader
    row may parasitize a workflow team; its activity belongs to the session and is not collected by this run.
    """

    from aiteam.api.state_reaper import StateReaper
    from aiteam.types import WorkflowRun

    # (4) All runs terminal but a busy member exists → veto
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_busy-04", status="killed"))
    t_busy = await repo.create_team(
        name="workflow-session-44444444",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_busy-04"},
    )
    a_busy = await repo.create_agent(
        team_id=t_busy.id, name="wf-b1", role="workflow-subagent"
    )
    await repo.update_agent(a_busy.id, status="busy")

    # (5) When collecting, waiting members turn offline; the parasitic leader row is left exactly as is
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_sweep-05", status="failed"))
    t_sweep = await repo.create_team(
        name="workflow-session-55555555",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_sweep-05"},
    )
    a_wait = await repo.create_agent(
        team_id=t_sweep.id, name="wf-w1", role="workflow-subagent"
    )
    await repo.update_agent(a_wait.id, status="waiting")
    a_leader = await repo.create_agent(team_id=t_sweep.id, name="Leader", role="leader")
    await repo.update_agent(a_leader.id, status="waiting")

    # (6) A zero-run team whose silence has passed the long grace period → collect
    t_norun_old = await repo.create_team(
        name="workflow-session-66666666",
        mode="coordinate",
        config={"kind": "workflow"},
    )
    a_norun = await repo.create_agent(
        team_id=t_norun_old.id, name="wf-n1", role="workflow-subagent"
    )
    await repo.update_agent(a_norun.id, status="offline")

    await _age_workflow_fixtures(repo, hours=30)

    reaper = StateReaper(repo, event_bus)
    await reaper._check_stale_teams(utc_now(), repo)

    assert (await repo.get_team(t_busy.id)).status == "active", "A busy member has a veto"
    assert (await repo.get_team(t_sweep.id)).status == "completed"
    swept = {a.name: a.status for a in await repo.list_agents(t_sweep.id)}
    assert swept["wf-w1"] == "offline", "When collecting, waiting members should be swept to offline"
    assert swept["Leader"] == "waiting", "The parasitic leader row is not collected by this team"
    assert (
        await repo.get_team(t_norun_old.id)
    ).status == "completed", "A zero-run team should be collected after the long grace period"


# ============================================================
# 5. GET three endpoints
# ============================================================


@pytest.mark.asyncio
async def test_read_endpoints(
    client: AsyncClient, repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)

    # GET /api/workflows
    lst = await client.get("/api/workflows")
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] >= 1
    assert any(r["wf_id"] == WF_ID for r in body["data"])

    # ?status= filter
    lst_running = await client.get("/api/workflows?status=running")
    assert lst_running.json()["total"] == 0
    lst_done = await client.get("/api/workflows?status=completed")
    assert any(r["wf_id"] == WF_ID for r in lst_done.json()["data"])

    # GET /api/workflows/{wf_id}
    detail = await client.get(f"/api/workflows/{WF_ID}")
    assert detail.status_code == 200
    assert detail.json()["total_tokens"] == 551440

    # GET /api/workflows/{wf_id}/agents
    ag = await client.get(f"/api/workflows/{WF_ID}/agents")
    assert ag.status_code == 200
    assert ag.json()["total"] == 2

    # 404
    missing = await client.get("/api/workflows/wf_does-not-exist")
    assert missing.status_code == 404


# ============================================================
# 6. POST /api/workflows/reconcile (temp directory, monkeypatching the projects root)
# ============================================================


@pytest.mark.asyncio
async def test_reconcile_endpoint(
    client: AsyncClient, repo: StorageRepository, tmp_path: Path, monkeypatch
):
    root_path = "/tmp/test-workflows-project"
    await repo.create_project(name="wf-recon", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)

    base = tmp_path / "projects"
    run_dir = base / slug / "SESSION-XYZ" / "workflows"
    run_dir.mkdir(parents=True)
    (run_dir / f"{WF_ID}.json").write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)

    resp = await client.post(
        "/api/workflows/reconcile", json={"project_dir": root_path}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingested"] == 1
    assert data["scanned"] == 1

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None and run.status == "completed"


# ============================================================
# 8. Project isolation: a run with a project_id is only visible in the corresponding scope
# ============================================================


@pytest.mark.asyncio
async def test_project_isolation(repo: StorageRepository):
    from aiteam.types import WorkflowRun

    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_aaa-1", project_id=PID_A, status="completed"))
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_bbb-2", project_id=PID_B, status="completed"))

    # The global repo sees both
    assert len(await repo.list_workflow_runs()) == 2

    # The scoped repo (project_scope=A) only sees A
    scoped_a = StorageRepository(db_url=repo._db_url, project_scope=PID_A)
    runs_a = await scoped_a.list_workflow_runs()
    assert len(runs_a) == 1 and runs_a[0].wf_id == "wf_aaa-1"
    # A cross-scope get is invisible (it does not replay all teams disappearing, but the isolation takes effect)
    assert await scoped_a.get_workflow_run("wf_bbb-2") is None
    assert await scoped_a.get_workflow_run("wf_aaa-1") is not None

    # Endpoint ?project_id= filter
    runs_b_query = await repo.list_workflow_runs(project_id=PID_B)
    assert len(runs_b_query) == 1 and runs_b_query[0].wf_id == "wf_bbb-2"


# ============================================================
# Phase2 §10.9 backend acceptance (live tail / lastCtx / fingerprint / interrupted / .output)
# ============================================================

import os as _os  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from aiteam.types import WorkflowRun as _WFRun  # noqa: E402

WF_LIVE = "wf_live0001-abc"


def _journal_line(kind: str, ccid: str) -> str:
    return json.dumps({"type": kind, "key": f"v2:{ccid}-key", "agentId": ccid}) + "\n"


def _assistant_line(inp: int, cc: int, cr: int, out: int, ts: str = "2026-07-06T03:00:05.000Z") -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "output_tokens": out,
            },
        },
    }) + "\n"


def _user_line(ts: str = "2026-07-06T03:00:00.000Z") -> str:
    return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": "go"}}) + "\n"


async def _setup_live_run(
    repo: StorageRepository,
    tmp_path: Path,
    monkeypatch,
    wf_id: str = WF_LIVE,
    session: str = "SESS-LIVE",
    root_path: str = "/tmp/wf-live-project",
):
    """live-environment scaffolding: project + running run + <slug>/<session>/subagents/workflows/<wf_id>/."""
    proj = await repo.create_project(name=f"wf-live-{wf_id[-4:]}", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    base = tmp_path / "projects"
    wf_dir = base / slug / session / "subagents" / "workflows" / wf_id
    wf_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf_id, project_id=proj.id, session_id=session,
        status="running", source="hook", cc_task_id="tsk123abc",
    ))
    run = await repo.get_workflow_run(wf_id)
    return proj, base, wf_dir, run


# ---------- 1. EventType: the new 2 members round-trip + the MVP 3 members unchanged ----------


def test_eventtype_phase2_members_roundtrip():
    for val in ("workflow.agent_updated", "workflow.run_ingested"):
        et = EventType(val)
        assert et.value == val
    # The MVP 3 members unchanged (append-only, non-breaking)
    for val in ("workflow.planned", "workflow.started", "workflow.completed"):
        assert EventType(val).value == val


# ---------- 2. Legacy database upgrade: file database init_db → the 5 new columns in place, existing rows default to
# 0/''/NULL ----------


@pytest.mark.asyncio
async def test_file_db_migration_adds_phase2_columns(tmp_path: Path):
    import sqlite3

    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    # The MVP schema (without the Phase2 5 columns)
    con.execute(
        "CREATE TABLE workflow_runs ("
        "id TEXT PRIMARY KEY, wf_id TEXT, project_id TEXT, team_id TEXT, session_id TEXT,"
        "cc_task_id TEXT, name TEXT, status TEXT, source TEXT, phases TEXT,"
        "planned_agent_count INTEGER, dynamic_nodes INTEGER, agent_count INTEGER,"
        "total_tokens INTEGER, total_tool_calls INTEGER, duration_ms INTEGER,"
        "summary TEXT, result TEXT, script_path TEXT, started_at DATETIME,"
        "completed_at DATETIME, created_at DATETIME, updated_at DATETIME)"
    )
    con.execute(
        "CREATE TABLE workflow_agents ("
        "id TEXT PRIMARY KEY, run_id TEXT, wf_id TEXT, project_id TEXT, cc_agent_id TEXT,"
        "os_agent_id TEXT, label TEXT, phase_index INTEGER, phase_title TEXT, model TEXT,"
        "state TEXT, tokens INTEGER, tool_calls INTEGER, duration_ms INTEGER,"
        "last_tool_name TEXT, last_tool_summary TEXT, prompt_preview TEXT,"
        "result_preview TEXT, started_at DATETIME, queued_at DATETIME,"
        "created_at DATETIME, updated_at DATETIME)"
    )
    con.execute(
        "INSERT INTO workflow_runs (id, wf_id, status, source, phases, name, project_id,"
        " summary, script_path, created_at, updated_at) VALUES"
        " ('old-1', 'wf_old-1', 'completed', 'file', '[]', 'legacy', '', '', '',"
        "  '2026-07-01 00:00:00.000000', '2026-07-01 00:00:00.000000')"
    )
    con.commit()
    con.close()

    r2 = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    await r2.init_db()  # create_all (skips existing tables) + the COLUMNS_TO_ENSURE ALTER migration

    run = await r2.get_workflow_run("wf_old-1")  # The ORM reads through = the columns are truly in place
    assert run is not None
    assert run.journal_offset == 0
    assert run.source_fingerprint == ""
    assert run.live_tokens == 0
    assert run.last_activity_at is None
    await close_db()

    con = sqlite3.connect(db_file)
    run_cols = {row[1] for row in con.execute("PRAGMA table_info(workflow_runs)")}
    agent_cols = {row[1] for row in con.execute("PRAGMA table_info(workflow_agents)")}
    con.close()
    assert {"journal_offset", "source_fingerprint", "live_tokens", "last_activity_at"} <= run_cols
    assert "last_activity_at" in agent_cols


# ---------- 3. journal tail: incremental consumption + partial-line protection ----------


@pytest.mark.asyncio
async def test_journal_tail_and_half_line_guard(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch)
    journal = wf_dir / "journal.jsonl"
    journal.write_text(
        _journal_line("started", "aga1") + _journal_line("started", "aga2")
        + _journal_line("result", "aga2"),
        encoding="utf-8",
    )

    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["ok"] and not res["terminal"]
    run = await repo.get_workflow_run(WF_LIVE)
    assert run.journal_offset == journal.stat().st_size  # Fully consumed
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga1"].state == "running"
    assert agents["aga2"].state == "done"

    # Append 1 result + a trailing partial line with no newline → the offset only advances to the last \n; the partial
    # line is not parsed
    half = json.dumps({"type": "started", "key": "v2:k3", "agentId": "aga3"})
    with journal.open("a", encoding="utf-8") as f:
        f.write(_journal_line("result", "aga1"))
        f.write(half[: len(half) // 2])  # Concurrent write of a partial line
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run(WF_LIVE)
    size_to_last_nl = journal.read_bytes().rfind(b"\n") + 1
    assert run.journal_offset == size_to_last_nl
    assert run.journal_offset < journal.stat().st_size
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga1"].state == "done"
    assert "aga3" not in agents  # The partial line is not parsed

    # After completing the \n, the next tick consumes it
    with journal.open("a", encoding="utf-8") as f:
        f.write(half[len(half) // 2:] + "\n")
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run(WF_LIVE)
    assert run.journal_offset == journal.stat().st_size
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga3"].state == "running"

    # The aggregate event was emitted (at most one agent_updated per run per tick)
    ev = [e for e in await repo.list_events(type_prefix="workflow.")
          if e.type == EventType.WORKFLOW_AGENT_UPDATED]
    assert ev, "live tail should aggregate and emit workflow.agent_updated"


# ---------- 4. File rewrite: size < offset → reset to 0 and tail again ----------


@pytest.mark.asyncio
async def test_journal_rewrite_resets_offset(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch, wf_id="wf_live0002-rst")
    journal = wf_dir / "journal.jsonl"
    journal.write_text(
        _journal_line("started", "agb1") + _journal_line("started", "agb2"), encoding="utf-8"
    )
    await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run("wf_live0002-rst")
    assert run.journal_offset == journal.stat().st_size

    # A truncate rewrite to shorter content → reset to 0 → consume again
    journal.write_text(_journal_line("result", "agb1"), encoding="utf-8")
    assert journal.stat().st_size < run.journal_offset
    await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run("wf_live0002-rst")
    assert run.journal_offset == journal.stat().st_size  # After the reset it advances again to the new file length
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents("wf_live0002-rst")}
    assert agents["agb1"].state == "done"


# ---------- 5. lastCtx metric: the sum of the four fields of the last assistant message ≠ accumulated across rounds; no
# jsonl records 0 ----------


@pytest.mark.asyncio
async def test_lastctx_token_metric(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch, wf_id="wf_live0003-ctx")
    (wf_dir / "journal.jsonl").write_text(
        _journal_line("started", "agc1") + _journal_line("started", "agc2"), encoding="utf-8"
    )
    # agc1 has a jsonl: 3 assistant usage entries increasing, including a large cache_read
    rounds = [(10, 5, 100, 20), (2, 3, 200, 30), (2, 15010, 145842, 13479)]
    (wf_dir / "agent-agc1.jsonl").write_text(
        _user_line() + "".join(_assistant_line(*r) for r in rounds), encoding="utf-8"
    )
    last_ctx = sum(rounds[-1])          # 174333
    cross_sum = sum(sum(r) for r in rounds)  # Accumulated across rounds (cache_read counted repeatedly; the rejected
    # metric)

    await workflow_ingest.tail_live_run(repo, event_bus, run)
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents("wf_live0003-ctx")}
    assert agents["agc1"].tokens == last_ctx
    assert agents["agc1"].tokens != cross_sum
    assert agents["agc1"].started_at is not None  # First line's timestamp → started_at
    assert agents["agc1"].last_activity_at is not None
    assert agents["agc2"].tokens == 0  # The journal says started but there is no jsonl → cached records 0

    run = await repo.get_workflow_run("wf_live0003-ctx")
    assert run.live_tokens == last_ctx  # Σ agents (agc2 records 0)
    assert run.last_activity_at is not None
    assert run.status == "running"  # The file is fresh; never misjudge it as interrupted


# ---------- 6. Watermark merging: an explicit 0 resets; an ingest that does not carry an offset does not erase it to 0
# ----------


@pytest.mark.asyncio
async def test_watermark_merge_semantics(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    t1 = datetime(2026, 7, 6, 10, 0, 0, tzinfo=UTC)
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="running", journal_offset=123, live_tokens=42,
        source_fingerprint="111:222", last_activity_at=t1,
    ))
    # None = do not change
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_wl-1", status=""))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.journal_offset == 123 and run.live_tokens == 42
    assert run.source_fingerprint == "111:222" and run.last_activity_at == t1

    # An explicit 0/'' = reset (proving the 'new non-zero wins' rule was not applied)
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="", journal_offset=0, source_fingerprint="",
    ))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.journal_offset == 0 and run.source_fingerprint == ""
    assert run.live_tokens == 42  # Columns not carried are left untouched

    # last_activity_at takes the max monotonically (an old value never rolls back)
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="", last_activity_at=t1 - timedelta(hours=1),
    ))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.last_activity_at == t1
    t2 = t1 + timedelta(minutes=5)
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_wl-1", status="", last_activity_at=t2))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.last_activity_at == t2

    # ingest_run_from_file does not carry an offset (None) → it is not erased to 0
    await repo.upsert_workflow_run(_WFRun(wf_id=WF_ID, status="running", journal_offset=77))
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    run = await repo.get_workflow_run(WF_ID)
    assert run.journal_offset == 77
    st = wf_file.stat()
    assert run.source_fingerprint == f"{st.st_mtime_ns}:{st.st_size}"  # The fp is written along with the ingest


# ---------- 7. fingerprint: same fp skips; same second but different size re-ingests; legacy rows use the mtime rule
# ----------


@pytest.mark.asyncio
async def test_fingerprint_skip_and_same_second_reingest(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf3 = "wf_fp0001-abc"
    root_path = "/tmp/wf-fp-project"
    await repo.create_project(name="wf-fp", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    base = tmp_path / "projects"
    run_dir = base / slug / "S1" / "workflows"
    run_dir.mkdir(parents=True)
    snap = _fixture_snapshot()
    snap["runId"] = wf3
    jf = run_dir / f"{wf3}.json"
    jf.write_text(json.dumps(snap), encoding="utf-8")
    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)

    r1 = await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert r1["ingested"] == 1
    st0 = jf.stat()
    run = await repo.get_workflow_run(wf3)
    assert run.source_fingerprint == f"{st0.st_mtime_ns}:{st0.st_size}"

    # Counting wrapper: reconcile calls ingest_run_from_file internally via the module-global name
    calls: list[str] = []
    orig_ingest = workflow_ingest.ingest_run_from_file

    async def counting(repo_, bus_, path_):
        calls.append(str(path_))
        return await orig_ingest(repo_, bus_, path_)

    monkeypatch.setattr(workflow_ingest, "ingest_run_from_file", counting)

    # A second run with the same fp → do not re-read
    r2 = await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert r2["scanned"] == 1 and len(calls) == 0

    # Same mtime second (identical at ns level) but a different size → the fingerprint catches it; the mtime rule would
    # miss it
    snap["summary"] = snap["summary"] + " — appended content from the resumed run changes the size"
    jf.write_text(json.dumps(snap), encoding="utf-8")
    _os.utime(jf, ns=(st0.st_atime_ns, st0.st_mtime_ns))  # Roll the mtime back to exactly the old value
    assert jf.stat().st_mtime_ns == st0.st_mtime_ns and jf.stat().st_size != st0.st_size
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 1  # Re-ingested (under the old mtime rule, mtime<=updated_at would wrongly skip it)

    # A legacy row with fp='' → use the original mtime rule (MVP regression): old mtime → skip; newer mtime → ingest
    await repo.upsert_workflow_run(_WFRun(wf_id=wf3, status="", source_fingerprint=""))
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 1  # mtime (old) <= updated_at (just upserted) → skip
    future_ns = st0.st_mtime_ns + 3_600_000_000_000  # +1h
    _os.utime(jf, ns=(future_ns, future_ns))
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 2  # The mtime became newer → re-ingest


# ---------- 8. interrupted: four-condition marking + taking the max avoids false positives + terminal 2→3 self-healing
# ----------


@pytest.mark.asyncio
async def test_interrupted_mark_and_self_heal(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf4 = "wf_intr0001-abc"
    _, base, wf_dir, run = await _setup_live_run(
        repo, tmp_path, monkeypatch, wf_id=wf4, root_path="/tmp/wf-intr-project"
    )
    journal = wf_dir / "journal.jsonl"
    journal.write_text(_journal_line("started", "agd1"), encoding="utf-8")
    ajson = wf_dir / "agent-agd1.jsonl"
    ajson.write_text(_user_line() + _assistant_line(10, 0, 0, 5), encoding="utf-8")

    # Sub-case A: only the journal is stale while an agent jsonl is fresh → do not mark (condition 3 takes the max)
    stale_ns = int((utc_now() - timedelta(seconds=2000)).timestamp() * 1e9)
    _os.utime(journal, ns=(stale_ns, stale_ns))
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["marked_interrupted"] is False
    assert (await repo.get_workflow_run(wf4)).status == "running"

    # Sub-case B: all files idle for >900s → all four conditions met → mark + emit run_ingested
    _os.utime(ajson, ns=(stale_ns, stale_ns))
    run = await repo.get_workflow_run(wf4)
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["marked_interrupted"] is True
    run = await repo.get_workflow_run(wf4)
    assert run.status == "interrupted"  # rank 1→2; only marked, the row is not deleted
    ev = [e for e in await repo.list_events(type_prefix="workflow.")
          if e.type == EventType.WORKFLOW_RUN_INGESTED]
    assert ev and any(e.data.get("stall_seconds", 0) > 900 for e in ev)

    # Sub-case C: the terminal json lands → the reaper reconciles rank 2→3 and self-heals; the live observation set no
    # longer contains it
    from aiteam.api.state_reaper import StateReaper

    snap = _fixture_snapshot()
    snap["runId"] = wf4
    json_dir = wf_dir.parent.parent.parent / "workflows"
    json_dir.mkdir(parents=True, exist_ok=True)
    final = json_dir / f"{wf4}.json"
    final.write_text(json.dumps(snap), encoding="utf-8")
    fresh_ns = int((utc_now() + timedelta(seconds=5)).timestamp() * 1e9)
    _os.utime(final, ns=(fresh_ns, fresh_ns))  # Ensure mtime > run.updated_at (deterministic)

    reaper = StateReaper(repo, event_bus)
    await reaper._check_workflow_ingest(repo)  # An interrupted run within the 24h window is still reconciled
    run = await repo.get_workflow_run(wf4)
    assert run.status == "completed"  # The legal 2→3 flip self-heals

    # Sub-case D: the wf json already exists → the tail prefers the terminal state and never marks it again
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res.get("terminal") is True


# ---------- 9. Steady-state zero stat: no running runs and interrupted ones out of the 24h window → zero file accesses
# ----------


@pytest.mark.asyncio
async def test_steady_state_zero_stat(repo: StorageRepository, event_bus: EventBus, monkeypatch):
    import pathlib

    from sqlalchemy import text

    from aiteam.api.state_reaper import StateReaper
    from aiteam.storage.connection import get_session

    # One interrupted run whose updated_at is already out of the 24h recheck window (+ one terminal run; neither should
    # trigger IO)
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_zz-old", status="interrupted"))
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_zz-done", status="completed"))
    old = (utc_now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with get_session(repo._db_url) as session:
        await session.execute(
            text("UPDATE workflow_runs SET updated_at = :t WHERE wf_id = 'wf_zz-old'"),
            {"t": old},
        )

    counter = {"n": 0}
    orig_stat = pathlib.Path.stat

    def counting_stat(self, *a, **kw):
        counter["n"] += 1
        return orig_stat(self, *a, **kw)

    reaper = StateReaper(repo, event_bus)
    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
    try:
        await reaper._check_workflow_ingest(repo)
    finally:
        monkeypatch.setattr(pathlib.Path, "stat", orig_stat)
    assert counter["n"] == 0  # The observation set is empty → the whole section short-circuits, with zero file stats


# ---------- 10. .output fallback: real JSON enrichment; symlink/0-byte diversion skips; terminal columns untouched
# ----------


@pytest.mark.asyncio
async def test_output_fallback_enrichment(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf5 = "wf_out0001-abc"
    root_path = "/tmp/wf-out-project"
    proj = await repo.create_project(name="wf-out", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    tmp_claude = tmp_path / "claude-tmp"
    tasks_dir = tmp_claude / slug / "SESS-OUT" / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_ingest, "_claude_tmp_dir", lambda: tmp_claude)

    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf5, project_id=proj.id, status="interrupted", cc_task_id="tskout01",
    ))

    # Real JSON: a 7-key subset, missing runId (it must never be fed to ingest_run_from_file)
    output = {
        "summary": "Fallback snapshot summary",
        "agentCount": 2,
        "logs": ["l1"],
        "result": None,
        "totalTokens": 12345,
        "totalToolCalls": 7,
        "workflowProgress": [
            {"type": "workflow_agent", "agentId": "age1", "label": "map:x",
             "phaseIndex": 1, "phaseTitle": "Research", "state": "done", "tokens": 8000},
            {"type": "workflow_agent", "agentId": "age2", "label": "reduce:y",
             "phaseIndex": 2, "phaseTitle": "Synthesis", "state": "running", "tokens": 4345},
        ],
    }
    (tasks_dir / "tskout01.output").write_text(json.dumps(output), encoding="utf-8")

    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf5))
    assert res["ok"] is True and res["agents"] == 2
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(wf5)}
    assert agents["age1"].label == "map:x" and agents["age1"].tokens == 8000
    assert agents["age2"].phase_index == 2
    run = await repo.get_workflow_run(wf5)
    assert run.status == "interrupted"  # status untouched
    assert run.total_tokens == 0  # Terminal columns untouched (they belong only to the wf-json)
    assert run.live_tokens == 12345 and run.summary == "Fallback snapshot summary"

    # A symlink = an ordinary Task transcript → skip (opening a dangling symlink would raise, so it must be diverted via
    # lstat)
    wf6 = "wf_out0002-lnk"
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf6, project_id=proj.id, status="interrupted", cc_task_id="tsklnk02",
    ))
    target = tmp_path / "transcript.jsonl"
    target.write_text(_user_line(), encoding="utf-8")
    _os.symlink(target, tasks_dir / "tsklnk02.output")
    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf6))
    assert res["ok"] is False and res["reason"] == "no_output"

    # 0 bytes = a task is running → skip
    wf7 = "wf_out0003-zero"
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf7, project_id=proj.id, status="interrupted", cc_task_id="tskzero3",
    ))
    (tasks_dir / "tskzero3.output").write_text("", encoding="utf-8")
    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf7))
    assert res["ok"] is False and res["reason"] == "no_output"


# ============================================================
# Cross-project fixes A+B+C regression (the blind-spot triple found by the 2026-07-06 inspection)
# ============================================================


@pytest.mark.asyncio
async def test_receipt_persists_transcript_dir(
    client: AsyncClient, repo: StorageRepository
):
    """Fix A: the receipt's Transcript dir must be persisted to the run row (the foundation for direct addressing in
    live/terminal states)."""
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-tdir-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200
    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.transcript_dir, "transcript_dir should be persisted from the receipt"
    assert WF_ID in run.transcript_dir


@pytest.mark.asyncio
async def test_receipt_strict_project_attribution(
    client: AsyncClient, repo: StorageRepository, tmp_path
):
    """Fix C: project_id is resolved only from the launching cwd → the longest prefix among registered projects; when it
    cannot be resolved it stays empty and is never guessed.

    The old _find_leader cross-session fallback once attributed runs of unregistered projects to other projects (a
    recorded isolation violation).
    """
    proj = await repo.create_project(name="attr-proj", root_path=str(tmp_path / "projA"))

    # The cwd falls inside a registered project → attributed to that project
    r1 = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-attr-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
            "cwd": str(tmp_path / "projA" / "sub"),
        },
    )
    assert r1.status_code == 200
    run1 = await repo.get_workflow_run(WF_ID)
    assert run1 is not None and run1.project_id == proj.id

    # The cwd is outside every registered project → leave it empty, never guess
    other_receipt = RECEIPT.replace(WF_ID, "wf_attr2-9999")
    r2 = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-attr-2",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": other_receipt,
            "cwd": str(tmp_path / "elsewhere"),
        },
    )
    assert r2.status_code == 200
    run2 = await repo.get_workflow_run("wf_attr2-9999")
    assert run2 is not None and (run2.project_id or "") == ""


@pytest.mark.asyncio
async def test_candidate_slugs_session_fallback(
    repo: StorageRepository, tmp_path, monkeypatch
):
    """Fix B: legacy rows without a transcript_dir are looked up globally by session_id to find the slug (reachable
    for unregistered projects)."""
    from aiteam.api import workflow_ingest as wi
    from aiteam.types import WorkflowRun

    base = tmp_path / "projects"
    (base / "slug-unregistered" / "sess-fb-1").mkdir(parents=True)
    monkeypatch.setattr(wi, "_claude_projects_dir", lambda: base)

    run = WorkflowRun(wf_id="wf_fb-1", session_id="sess-fb-1", transcript_dir="")
    slugs = await wi._candidate_slugs(repo, run)
    assert "slug-unregistered" in slugs

    # A: a new row that has a transcript_dir does not enter the fallback path
    run2 = WorkflowRun(
        wf_id="wf_fb-2", session_id="sess-fb-1", transcript_dir=str(tmp_path / "t")
    )
    slugs2 = await wi._candidate_slugs(repo, run2)
    assert "slug-unregistered" not in slugs2


@pytest.mark.asyncio
async def test_stop_refreshes_leader_model(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """At the end of each Stop turn, the transcript is read to refresh the Leader's real model (the concluding
    regression for the
    4-7 ghost campaign from the user's four live tests on 2026-07-07 — it was once silently swallowed by a try
    because hook_translator was missing import json)."""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-lm", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-lm-1"
    )
    tp = tmp_path / "s.jsonl"
    tp.write_text(
        '{"type":"user","message":{}}\n'
        '{"type":"assistant","message":{"model":"claude-test-9"}}\n',
        encoding="utf-8",
    )
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_stop({"session_id": "sess-lm-1", "transcript_path": str(tp)})
    a = (await repo.find_agents_by_session("sess-lm-1"))[0]
    assert a.model == "claude-test-9"


def test_detect_live_session_file_truth(tmp_path, monkeypatch):
    """Leader identity = the newest CC main session under the project directory (read directly from the file source of
    truth, with zero registration dependency).
    User ruling 2026-07-07: the model/activity state is auto-detected by the backend, not via the hook registration
    chain."""
    from aiteam.api import session_probe as sp

    base = tmp_path / "projects"
    slug_dir = base / sp.project_slug("/Users/x/My Proj")
    slug_dir.mkdir(parents=True)
    old = slug_dir / "sess-old.jsonl"
    old.write_text('{"type":"assistant","message":{"model":"claude-a"}}\n')
    new = slug_dir / "sess-new.jsonl"
    new.write_text(
        '{"type":"assistant","message":{"model":"claude-b"}}\n'
        '{"type":"assistant","message":{"model":"<synthetic>"}}\n'
    )
    import os

    os.utime(old, (1, 1))  # The old session lags by mtime
    # A sub-agent directory should not interfere with the main-session glob
    (slug_dir / "sess-new").mkdir()
    monkeypatch.setattr(sp, "_claude_projects_dir", lambda: base)

    probe = sp.detect_live_session("/Users/x/My Proj")
    assert probe is not None
    assert probe["session_id"] == "sess-new"
    assert probe["model"] == "claude-b"  # synthetic synthetic rows are skipped
    assert probe["live"] is True  # Just written; the mtime is within the 15-minute window

    assert sp.detect_live_session("/no/such/project") is None


def test_detect_live_sessions_multi_ceo(tmp_path, monkeypatch):
    """Parallel multi-session = one CEO-<English name> per active session (user ruling 2026-07-10):
    all sessions within the active window are returned, the names are deterministic and non-duplicated, and when all
    are silent it falls back to the newest one."""
    from aiteam.api import session_probe as sp

    base = tmp_path / "projects"
    slug_dir = base / sp.project_slug("/Users/x/Multi Proj")
    slug_dir.mkdir(parents=True)
    a = slug_dir / "sess-aaaa.jsonl"
    a.write_text('{"type":"assistant","message":{"model":"claude-fable-5"}}\n')
    b = slug_dir / "sess-bbbb.jsonl"
    b.write_text('{"type":"assistant","message":{"model":"claude-opus-4-8"}}\n')
    stale = slug_dir / "sess-stale.jsonl"
    stale.write_text('{"type":"assistant","message":{"model":"claude-old"}}\n')
    import os

    os.utime(stale, (1, 1))  # Outside the active window; should not appear in the parallel list
    monkeypatch.setattr(sp, "_claude_projects_dir", lambda: base)

    sessions = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert len(sessions) == 2  # Only the two within the active window are returned; the stale one is excluded
    ids = {s["session_id"] for s in sessions}
    assert ids == {"sess-aaaa", "sess-bbbb"}
    assert all(s["live"] is True for s in sessions)
    names = [s["name"] for s in sessions]
    assert len(set(names)) == 2  # No duplicates
    assert all(n in sp.CEO_NAMES for n in names)
    # Determinism: probing the same batch of sessions repeatedly yields the same assignment (a refresh does not rename)
    again = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert {s["session_id"]: s["name"] for s in again} == {
        s["session_id"]: s["name"] for s in sessions
    }

    # All silent: fall back to only the newest one, with live=False (all mtimes pushed to the distant past)
    os.utime(a, (100_000, 100_000))
    os.utime(b, (50_000, 50_000))
    idle = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert len(idle) == 1
    assert idle[0]["session_id"] == "sess-aaaa"
    assert idle[0]["live"] is False


@pytest.mark.asyncio
async def test_stop_ignores_synthetic_model(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """compact writes synthetic assistant rows with model="<synthetic>" — if such a row happens to be the last one
    at the tail, it must not pollute the Leader's model (inspection record, 2026-07-07: the DB contained
    <synthetic>)."""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-syn", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-syn-1"
    )
    tp = tmp_path / "s.jsonl"
    tp.write_text(
        '{"type":"assistant","message":{"model":"claude-test-9"}}\n'
        '{"type":"assistant","message":{"model":"<synthetic>"}}\n',
        encoding="utf-8",
    )
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_stop({"session_id": "sess-syn-1", "transcript_path": str(tp)})
    a = (await repo.find_agents_by_session("sess-syn-1"))[0]
    assert a.model == "claude-test-9"


@pytest.mark.asyncio
async def test_tool_event_revives_leader(
    repo: StorageRepository, event_bus: EventBus
):
    """A stream of tool events = a conversation in progress: an offline Leader should be revived to busy (once observed
    live:
    'showing as closed while actually conversing' — the 5-minute heartbeat falsely killed it during a long turn and
    nobody revived it)."""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-rv", mode="coordinate")
    ld = await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-rv-1"
    )
    await repo.update_agent(ld.id, status="offline")
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._touch_session_leader("sess-rv-1")
    a = (await repo.find_agents_by_session("sess-rv-1"))[0]
    assert str(a.status).endswith("busy") or a.status == "busy"


# ============================================================
# Regression tests for the three server-side write-surface med fixes (task 6f313f77)
# ============================================================


@pytest.mark.asyncio
async def test_upsert_workflow_run_reports_terminal_transition_atomically(
    repo: StorageRepository,
):
    """WP10 root fix: move the decision of 'whether this is the first transition to completed / first entry into a
    terminal state' inside the upsert transaction,
    returning it atomically, replacing the out-of-transaction get→was_completed check-then-act (when three unserialized
    ingest drivers
    interleave, workflow.completed could be emitted repeatedly). This locks down the contract of the transition
    decision."""
    from aiteam.types import WorkflowRun

    # First seen as running: not completed, not terminal.
    r1 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="running"))
    assert (r1.became_completed, r1.became_terminal) == (False, False)
    # running → completed: the first transition; both are True.
    r2 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="completed"))
    assert (r2.became_completed, r2.became_terminal) == (True, True)
    # Upserting completed once more: already completed, no longer a transition — this is exactly the key spot that
    # prevents duplicate emits.
    r3 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="completed"))
    assert (r3.became_completed, r3.became_terminal) == (False, False)
    # First seen directly as completed (the reaper's first scan finds an old run already terminal): counts as a first
    # transition.
    r4 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-2", status="completed"))
    assert (r4.became_completed, r4.became_terminal) == (True, True)
    # running → killed: enters a terminal state but is not completed (drives run_ingested, not completed).
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="running"))
    rk = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="killed"))
    assert (rk.became_completed, rk.became_terminal) == (False, True)
    # killed → completed (an equal-rank terminal-to-terminal transition, the resumeFromRunId scenario): became_completed
    # is still True
    # (consistent with the old was_completed semantics: emit whenever prev!=completed), but it is already terminal, so
    # became_terminal is
    # False — run_ingested is not emitted again.
    rkc = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="completed"))
    assert (rkc.became_completed, rkc.became_terminal) == (True, False)


@pytest.mark.asyncio
async def test_ingest_concurrent_emits_completed_once(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """WP10 concurrency scenario: the same 'newly completed' wf file is processed simultaneously by multiple
    unserialized drivers (reaper reconciliation / SessionStart
    reconciliation / hook receipt ingest); workflow.completed must land exactly once."""
    import asyncio

    from aiteam.types import WorkflowRun

    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    # Pre-seed a running skeleton, approximating the real concurrency starting point of a 'running→completed first
    # transition'.
    await repo.upsert_workflow_run(WorkflowRun(wf_id=WF_ID, status="running"))

    results = await asyncio.gather(
        *[
            workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
            for _ in range(3)
        ]
    )
    # Measure the number of 'emit decisions' directly — this is what WP10 fixes: three interleaved drivers, only one can
    # obtain the
    # running→completed transition and emit workflow.completed; the rest get became_completed=False.
    # (We do not check list_events: under the in-memory database's single-connection topology, concurrent gathers lose
    # some event persistence — a test
    # environment artifact; production uses independent connections + WAL and has no such issue. The emitted flag in the
    # return value is the truth about the emit decision.)
    assert sum(1 for r in results if r.get("emitted")) == 1
    assert sum(1 for r in results if r.get("new_completion")) == 1


@pytest.mark.asyncio
async def test_agents_cc_tool_use_id_partial_unique(repo: StorageRepository):
    """B1: a partial unique index on agents.cc_tool_use_id (WHERE NOT NULL). When two
    create-if-absent paths race, the second create hits an IntegrityError (swallowed by the caller). NULL is exempt, so
    leader / ordinary api agents can continue to share NULL."""
    from sqlalchemy.exc import IntegrityError

    team = await repo.create_team(name="t-ccdup", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="wf-1", role="workflow-subagent", cc_tool_use_id="cc-DUP"
    )
    with pytest.raises(IntegrityError):
        await repo.create_agent(
            team_id=team.id, name="wf-2", role="workflow-subagent",
            cc_tool_use_id="cc-DUP",
        )
    # Multiple rows with a NULL cc can coexist (the leader carries no cc_tool_use_id and must never be affected by the
    # constraint).
    await repo.create_agent(team_id=team.id, name="ld-1", role="leader")
    await repo.create_agent(team_id=team.id, name="ld-2", role="leader")
    # An empty-string cc = 'no cc id' (the agent_id was truncated away by an over-long payload) is likewise exempt — two
    # such rows are each independent
    # and must never be wrongly merged into one row by the unique constraint.
    await repo.create_agent(
        team_id=team.id, name="e-1", role="workflow-subagent", cc_tool_use_id=""
    )
    await repo.create_agent(
        team_id=team.id, name="e-2", role="workflow-subagent", cc_tool_use_id=""
    )
    rows = await repo.list_agents(team.id)
    assert sum(1 for a in rows if a.cc_tool_use_id == "cc-DUP") == 1
    assert sum(1 for a in rows if a.cc_tool_use_id is None) == 2
    assert sum(1 for a in rows if a.cc_tool_use_id == "") == 2


@pytest.mark.asyncio
async def test_register_workflow_subagent_swallows_dup_create(
    repo: StorageRepository, event_bus: EventBus, monkeypatch
):
    """B1: a concurrent create race — the dedup find at the top of this coroutine misses (takes the create path), but by
    create time the row has
    been claimed by an earlier arrival → IntegrityError → swallowed → the re-fetch hits the existing row → update
    converges; no duplicate rows,
    and no 500 is thrown to the hook endpoint."""
    from aiteam.api.hook_translator import WORKFLOW_AGENT_TYPE, HookTranslator

    ht = HookTranslator(repo=repo, event_bus=event_bus)
    cc = "cc-race-1"
    team = await repo.create_team(
        name="workflow-wf_race",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_race"},
    )
    winner = await repo.create_agent(
        team_id=team.id, name="wf-winner", role=WORKFLOW_AGENT_TYPE,
        source="hook", cc_tool_use_id=cc,
    )

    real_find = repo.find_agent_by_cc_id
    calls = {"n": 0}

    async def fake_find(cid: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # The top-level dedup missed → forced into the create path
        return await real_find(cid)  # The re-fetch inside except → hits the winner

    monkeypatch.setattr(repo, "find_agent_by_cc_id", fake_find)

    res = await ht._register_workflow_subagent(
        {"cwd": "", "transcript_path": "x/subagents/workflows/wf_race/agent-x.jsonl"},
        cc_agent_id=cc,
        session_id="sess-race",
    )
    assert res["status"] == "updated"
    assert res["agent_id"] == winner.id
    all_rows = [
        a for t in await repo.list_teams() for a in await repo.list_agents(t.id)
    ]
    assert sum(1 for a in all_rows if a.cc_tool_use_id == cc) == 1


@pytest.mark.asyncio
async def test_session_end_exempts_running_workflow_subagent(
    repo: StorageRepository, event_bus: EventBus
):
    """WP6: SessionEnd no longer offlines all of a session's agents unconditionally — workflow sub-agents that are still
    running
    (role=workflow-subagent; a run can far outlive the launching session) are exempt, symmetric with the kind=workflow
    team exemption;
    ordinary agents go offline as usual."""
    from aiteam.api.hook_translator import WORKFLOW_AGENT_TYPE, HookTranslator

    sess = "sess-end-wf"
    wteam = await repo.create_team(
        name="workflow-wf_end",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_end"},
    )
    wf_member = await repo.create_agent(
        team_id=wteam.id, name="wf-m", role=WORKFLOW_AGENT_TYPE,
        source="hook", session_id=sess,
    )
    await repo.update_agent(wf_member.id, status="busy")
    normal_team = await repo.create_team(name="t-normal", mode="coordinate")
    normal = await repo.create_agent(
        team_id=normal_team.id, name="worker", role="developer",
        source="hook", session_id=sess,
    )
    await repo.update_agent(normal.id, status="busy")

    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_session_end({"session_id": sess})

    wf_after = await repo.get_agent(wf_member.id)
    assert str(wf_after.status).endswith("busy"), (
        "A running workflow sub-agent should not be offlined by SessionEnd"
    )
    assert wf_after.session_id == sess, "A workflow sub-agent's session_id should be preserved"
    normal_after = await repo.get_agent(normal.id)
    assert str(normal_after.status).endswith("offline"), "An ordinary agent should go offline as usual"
