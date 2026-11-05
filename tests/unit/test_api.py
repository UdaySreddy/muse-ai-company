"""AI Team OS — API unit tests.

Uses FastAPI TestClient and in-memory SQLite to test the REST API.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from testlib import make_team

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """Create a test client using in-memory SQLite."""
    import asyncio

    # Initialize the dependencies
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    # Inject into the deps module (including EventBus and HookTranslator)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    # Override the lifespan: no automatic init/cleanup is needed in tests
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    # Cleanup
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


# ============================================================
# Team CRUD
# ============================================================


def test_list_teams(app_client):
    """Test listing teams."""
    # Create two teams first
    make_team({"name": "team-a"})
    make_team({"name": "team-b"})

    resp = app_client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] >= 2


def test_get_team(app_client):
    """Test getting team details."""
    team_name = make_team({"name": "get-team"})["name"]

    resp = app_client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "get-team"


def test_update_team(app_client):
    """Test updating the team mode."""
    team_name = make_team({"name": "update-team"})["name"]

    resp = app_client.put(f"/api/teams/{team_name}", json={"mode": "broadcast"})
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "broadcast"


def test_delete_team(app_client):
    """Test deleting a team."""
    team_name = make_team({"name": "del-team"})["name"]

    resp = app_client.delete(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # Querying again should 404
    resp = app_client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 404


def test_get_team_status(app_client):
    """Test getting the team status."""
    team_name = make_team({"name": "status-team"})["name"]

    resp = app_client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["team"]["name"] == "status-team"
    assert data["completed_tasks"] == 0


# ============================================================
# Agent management
# ============================================================


def test_create_agent(app_client):
    """Test adding an Agent."""
    team_name = make_team({"name": "agent-team"})["name"]

    resp = app_client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "Backend Development"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["data"]["name"] == "dev-1"
    assert data["data"]["role"] == "Backend Development"


def test_list_agents(app_client):
    """Test listing Agents."""
    agents_list_team = make_team({"name": "agents-list-team"})
    team_id = agents_list_team["id"]

    app_client.post(
        f"/api/teams/{agents_list_team['name']}/agents",
        json={"name": "a1", "role": "Frontend"},
    )
    app_client.post(
        f"/api/teams/{agents_list_team['name']}/agents",
        json={"name": "a2", "role": "Backend"},
    )

    resp = app_client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def test_delete_agent(app_client):
    """Test deleting an Agent."""
    team_name = make_team({"name": "del-agent-team"})["name"]

    agent_resp = app_client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "to-delete", "role": "Testing"},
    )
    agent_id = agent_resp.json()["data"]["id"]

    resp = app_client.delete(f"/api/agents/{agent_id}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True


# ============================================================
# Task management
# ============================================================


def test_list_tasks(app_client):
    """Test listing tasks (empty list)."""
    team_id = make_team({"name": "task-team"})["id"]

    resp = app_client.get(f"/api/teams/{team_id}/tasks")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_run_task_mock(app_client):
    """Test running a task (creating a task record)."""
    team_name = make_team({"name": "run-team"})["name"]

    resp = app_client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"description": "Test task", "title": "Test"},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["title"] == "Test"
    assert "id" in data


def test_get_task_status(app_client):
    """Test querying a task's status."""
    team_name = make_team({"name": "task-status-team"})["name"]

    run_resp = app_client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"description": "Status query test", "title": "Status query"},
    )

    task_id = run_resp.json()["data"]["id"]
    resp = app_client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == task_id


# ============================================================
# Events
# ============================================================


def test_list_events(app_client):
    """Test listing events (empty list)."""
    resp = app_client.get("/api/events")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_list_events_with_filter(app_client):
    """Test event filter parameters."""
    resp = app_client.get("/api/events?type=team.created&source=api&limit=10")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ============================================================
# Memories
# ============================================================


def test_search_memories(app_client):
    """Test searching memories."""
    resp = app_client.get("/api/memory?scope=global&scope_id=system")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ============================================================
# Error handling
# ============================================================


def test_error_team_not_found(app_client):
    """Test that a non-existent team returns 404."""
    resp = app_client.get("/api/teams/nonexistent-id-12345")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "not_found"


def test_error_task_not_found(app_client):
    """Test that a non-existent task returns 404."""
    resp = app_client.get("/api/tasks/nonexistent-task-id")
    assert resp.status_code == 404


# ============================================================
# CORS and OpenAPI
# ============================================================


def test_cors_headers(app_client):
    """Test CORS response headers."""
    resp = app_client.options(
        "/api/teams",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_openapi_docs_accessible(app_client):
    """Test that the OpenAPI documentation is accessible."""
    resp = app_client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["title"] == "AI Team OS"
    assert "/api/teams" in data["paths"]


# ============================================================
# Hook schema validation
# ============================================================


def test_hook_event_valid_payload(app_client):
    """Test that the Hook endpoint accepts a valid payload."""
    resp = app_client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "SubagentStart",
            "session_id": "sess-123",
            "agent_id": "agent-1",
            "agent_type": "code",
            "tool_name": "",
            "cwd": "/tmp/project",
            "cc_team_name": "my-team",
        },
    )
    assert resp.status_code == 200


def test_hook_event_empty_payload(app_client):
    """Test that the Hook endpoint accepts an empty payload (all fields have defaults)."""
    resp = app_client.post("/api/hooks/event", json={})
    assert resp.status_code == 200


def test_hook_event_extra_fields_allowed(app_client):
    """Test that the Hook endpoint allows extra fields (backward compatibility)."""
    resp = app_client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "unknown_future_field": "some_value",
            "another_field": 42,
        },
    )
    assert resp.status_code == 200


def test_hook_event_field_too_long(app_client):
    """Test that the Hook endpoint rejects over-long fields."""
    resp = app_client.post(
        "/api/hooks/event",
        json={"hook_event_name": "x" * 100},  # max_length=50
    )
    assert resp.status_code == 422


def test_hook_event_invalid_type(app_client):
    """Test that the Hook endpoint rejects wrongly-typed fields."""
    resp = app_client.post(
        "/api/hooks/event",
        json={"tool_input": "not_a_dict"},
    )
    assert resp.status_code == 422
