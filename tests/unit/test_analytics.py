"""AI Team OS — Analytics API tests.

Tests the activity analytics statistics endpoints: tool usage distribution, Agent productivity, timeline, team overview.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from testlib import make_team

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """Create a test client using in-memory SQLite."""
    import asyncio

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)

    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


def _setup_team_with_activities(client) -> tuple[str, str]:
    """Create team, Agent and activity data; returns (team_id, agent_id)."""
    # Create a team
    analytics_team = make_team({"name": "analytics-team"})
    team_id = analytics_team["id"]
    team_name = analytics_team["name"]

    # Add an Agent
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "Development"},
    )
    agent_id = resp.json()["data"]["id"]

    # Add activity records
    import asyncio

    repo = deps._repository

    async def add_activities():
        await repo.create_activity(agent_id, "session-1", "Bash", "ls -la", "file list")
        await repo.create_activity(agent_id, "session-1", "Read", "main.py", "content...")
        await repo.create_activity(agent_id, "session-1", "Bash", "pytest", "passed")
        await repo.create_activity(agent_id, "session-1", "Edit", "main.py", "updated")
        await repo.create_activity(agent_id, "session-1", "Read", "config.py", "content...")

    asyncio.get_event_loop().run_until_complete(add_activities())
    return team_id, agent_id


# ============================================================
# Tool usage distribution
# ============================================================


def test_tool_usage_empty(app_client):
    """With no activity data, returns an empty list."""
    resp = app_client.get("/api/analytics/tool-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"] == []


def test_tool_usage_with_data(app_client):
    """With activity data, returns the tool distribution sorted by count."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/tool-usage")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 3  # Bash, Read, Edit
    # Bash and Read twice each (the order is uncertain), Edit once
    counts = {item["tool_name"]: item["count"] for item in data}
    assert counts["Bash"] == 2
    assert counts["Read"] == 2
    assert counts["Edit"] == 1


def test_tool_usage_filter_by_team(app_client):
    """Filter tool usage by team."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/tool-usage?team_id={team_id}")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3


# ============================================================
# Agent productivity
# ============================================================


def test_agent_productivity_empty(app_client):
    """With no data, returns empty."""
    resp = app_client.get("/api/analytics/agent-productivity")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_agent_productivity_with_data(app_client):
    """With data, returns each Agent's productivity metrics."""
    team_id, agent_id = _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/agent-productivity")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["agent_name"] == "dev-1"
    assert data[0]["activity_count"] == 5
    assert data[0]["tools_used"] == 3  # Bash, Read, Edit
    assert data[0]["last_active"] is not None


# ============================================================
# Activity timeline
# ============================================================


def test_timeline_empty(app_client):
    """With no data, returns empty."""
    resp = app_client.get("/api/analytics/timeline")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_timeline_with_data(app_client):
    """With data, aggregates by hour."""
    _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/timeline?hours=1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # All activities are within the same hour
    assert len(data) >= 1
    total = sum(item["count"] for item in data)
    assert total == 5


def test_timeline_hours_validation(app_client):
    """The hours parameter range limit (1-168)."""
    resp = app_client.get("/api/analytics/timeline?hours=0")
    assert resp.status_code == 422  # validation error

    resp = app_client.get("/api/analytics/timeline?hours=200")
    assert resp.status_code == 422


# ============================================================
# Team overview
# ============================================================


def test_team_overview_requires_team_id(app_client):
    """team_id is a required parameter."""
    resp = app_client.get("/api/analytics/team-overview")
    assert resp.status_code == 422


def test_team_overview_with_data(app_client):
    """Returns the complete team overview statistics."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/team-overview?team_id={team_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_activities"] == 5
    assert data["total_agents"] == 1
    assert isinstance(data["tool_distribution"], list)
    assert isinstance(data["agent_productivity"], list)


# ============================================================
# Efficiency metrics
# ============================================================


def test_efficiency_empty(app_client):
    """With no data, returns zero-valued efficiency metrics."""
    resp = app_client.get("/api/analytics/efficiency")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["task_completion"]["total_tasks"] == 0
    assert data["task_completion"]["completion_rate"] == 0
    assert data["avg_tools_per_task"] is None
    assert data["agent_utilization"] == []
    assert data["top_agents"] == []


def test_efficiency_with_tasks(app_client):
    """With task and activity data, returns the correct efficiency metrics."""
    team_id, agent_id = _setup_team_with_activities(app_client)

    # Create a few tasks and complete some of them
    import asyncio

    repo = deps._repository

    async def add_tasks():
        t1 = await repo.create_task(team_id, "Task 1", assigned_to=agent_id)
        t2 = await repo.create_task(team_id, "Task 2", assigned_to=agent_id)
        await repo.create_task(team_id, "Task 3")
        # Complete t1 and t2
        await repo.update_task(t1.id, status="completed", completed_at=utc_now())
        await repo.update_task(t2.id, status="completed", completed_at=utc_now())

    asyncio.get_event_loop().run_until_complete(add_tasks())

    resp = app_client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]

    # Task completion rate: 2/3
    tc = data["task_completion"]
    assert tc["total_tasks"] == 3
    assert tc["completed_tasks"] == 2
    assert 0.66 <= tc["completion_rate"] <= 0.67

    # Average tool calls / task: 5 activities / 2 completed tasks = 2.5
    assert data["avg_tools_per_task"] == 2.5

    # Agent utilization rate
    assert len(data["agent_utilization"]) == 1
    assert data["agent_utilization"][0]["agent_name"] == "dev-1"
    assert data["agent_utilization"][0]["activity_count"] == 5

    # Most-efficient ranking
    assert len(data["top_agents"]) == 1


def test_efficiency_filter_by_team(app_client):
    """Filter efficiency metrics by team."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["agent_utilization"][0]["agent_name"] == "dev-1"

    # Query a non-existent team
    resp = app_client.get("/api/analytics/efficiency?team_id=nonexistent")
    assert resp.status_code == 200
    assert resp.json()["data"]["agent_utilization"] == []
