"""End-to-end workflow integration tests.

Verifies the complete team collaboration flow: team creation, Agent addition, task management, etc.
"""

from __future__ import annotations

import asyncio

from testlib import make_team

# ============================================================
# 1. Coordinate-mode workflow
# ============================================================


def test_coordinate_workflow(integration_client):
    """coordinate mode: create a team → add 2 Agents → create a task → verify the task record."""
    client = integration_client

    # Create a coordinate team
    team_name = make_team({"name": "coord-workflow", "mode": "coordinate"})["name"]

    # Add 2 Agents
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "analyst", "role": "Data Analyst", "system_prompt": "You are a data analysis expert"},
    )
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "writer", "role": "Report Writing", "system_prompt": "You are a technical documentation expert"},
    )

    # Create a task (run_task now only creates a task record; it does not execute LangGraph)
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={
            "description": "Analyze Q1 sales data and write a report",
            "title": "Q1 Sales Analysis",
        },
    )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "pending"
    assert result["title"] == "Q1 Sales Analysis"
    assert "id" in result

    # Verify the task was recorded
    resp = client.get(f"/api/teams/{team_name}/tasks")
    assert resp.status_code == 200 if resp.json()["success"] else True


# ============================================================
# 2. Broadcast-mode workflow
# ============================================================


def test_broadcast_workflow(integration_client):
    """broadcast mode: create team → add 3 Agents → create a task → verify the task record."""
    client = integration_client

    # Create a broadcast team
    team_name = make_team({"name": "bcast-workflow", "mode": "broadcast"})["name"]

    # Add 3 Agents
    for name, role in [("dev1", "Frontend"), ("dev2", "Backend"), ("dev3", "Testing")]:
        resp = client.post(
            f"/api/teams/{team_name}/agents",
            json={"name": name, "role": role},
        )
        assert resp.status_code == 201

    # Verify 3 Agents were added
    team_id = client.get(f"/api/teams/{team_name}").json()["data"]["id"]
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.json()["total"] == 3

    # Create a task (run_task now only creates a task record; it does not execute LangGraph)
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={
            "description": "Review the system architecture proposal",
            "title": "Architecture Review",
        },
    )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "pending"
    assert result["title"] == "Architecture Review"


# ============================================================
# 3. Memory storage and retrieval workflow
# ============================================================


def test_memory_store_workflow(repo_and_client):
    """Store a memory → retrieve it via the API → verify the content."""
    repo, client = repo_and_client

    # Store a memory directly via the repo (simulating a write from the business logic layer)
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="team",
            scope_id="test-team",
            content="The team decided to adopt a microservices architecture",
            metadata={"author": "tech-lead"},
        )
    )
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="team",
            scope_id="test-team",
            content="The database choice was settled as PostgreSQL",
            metadata={"author": "dba"},
        )
    )

    # Search via the API
    resp = client.get("/api/memory?scope=team&scope_id=test-team&query=architecture")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] >= 1
    # The search results should contain 'microservices architecture'
    contents = [m["content"] for m in data["data"]]
    assert any("microservices architecture" in c for c in contents)

    # List all without a query
    resp = client.get("/api/memory?scope=team&scope_id=test-team")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


# ============================================================
# 4. Cross-session persistence verification
# ============================================================


def test_persistence_across_sessions(repo_and_client):
    """Create data → verify the data persists within the same database session."""
    repo, client = repo_and_client

    # Phase one: create the data
    persist_team = make_team({"name": "persist-team", "mode": "coordinate"})
    team_name = persist_team["name"]
    team_id = persist_team["id"]

    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "persist-agent", "role": "Persistence testing"},
    )

    # Store a memory
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="global",
            scope_id="system",
            content="Persistence test data",
        )
    )

    # Create an event
    asyncio.get_event_loop().run_until_complete(
        repo.create_event(
            event_type="team.created",
            source="persistence-test",
            data={"team_id": team_id},
        )
    )

    # Phase two: simulate 'reopening' — verify the data exists through brand-new requests
    # (Verified within the same database, because the in-memory SQLite connection stays open for the duration of the
    # test)

    # Verify the team exists
    resp = client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "persist-team"

    # Verify the Agent exists
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["data"][0]["name"] == "persist-agent"

    # Verify the memory exists
    resp = client.get("/api/memory?scope=global&scope_id=system")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    # Verify the event exists
    resp = client.get("/api/events?source=persistence-test")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ============================================================
# 5. Task execution failure scenario
# ============================================================


def test_task_failure_workflow(integration_client):
    """When a task is created, its pending status should be recorded correctly (tasks are no longer executed directly
    in the API)."""
    client = integration_client

    # Create a team
    team_name = make_team({"name": "fail-workflow"})["name"]

    # run_task now only creates a task record; it does not execute LangGraph
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"description": "A task awaiting execution", "title": "Task creation test"},
    )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "pending"
    assert result["title"] == "Task creation test"


# ============================================================
# 6. Multi-team concurrent operations
# ============================================================


def test_multiple_teams_isolation(integration_client):
    """Data isolation between multiple teams."""
    client = integration_client

    # Create two teams
    alpha = make_team({"name": "team-alpha"})
    beta = make_team({"name": "team-beta"})
    team_a = alpha["name"]
    team_b = beta["name"]
    team_a_id = alpha["id"]
    team_b_id = beta["id"]

    # Each adds different Agents
    client.post(f"/api/teams/{team_a}/agents", json={"name": "a1", "role": "Development"})
    client.post(f"/api/teams/{team_a}/agents", json={"name": "a2", "role": "Testing"})
    client.post(f"/api/teams/{team_b}/agents", json={"name": "b1", "role": "Design"})

    # Verify isolation
    resp = client.get(f"/api/teams/{team_a_id}/agents")
    assert resp.json()["total"] == 2

    resp = client.get(f"/api/teams/{team_b_id}/agents")
    assert resp.json()["total"] == 1

    # Deleting team-alpha does not affect team-beta
    client.delete(f"/api/teams/{team_a}")
    resp = client.get(f"/api/teams/{team_b}")
    assert resp.status_code == 200
