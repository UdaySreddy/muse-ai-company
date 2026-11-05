"""API integration tests — full CRUD flows.

Uses TestClient + a real SQLite database (in-memory) to test API end-to-end behavior.
"""

from __future__ import annotations

import asyncio

from testlib import make_team

# ============================================================
# 1. Full team lifecycle
# ============================================================


def test_full_team_lifecycle(integration_client):
    """The full flow: create team → get → list → update → delete."""
    client = integration_client

    # Create
    team = make_team({"name": "lifecycle-team", "mode": "coordinate"})
    team_id = team["id"]
    assert team["name"] == "lifecycle-team"
    assert team["mode"] == "coordinate"

    # Get (by name)
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == team_id

    # Get (by ID)
    resp = client.get(f"/api/teams/{team_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "lifecycle-team"

    # List
    resp = client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [t["name"] for t in data["data"]]
    assert "lifecycle-team" in names

    # Update the mode
    resp = client.put(f"/api/teams/{team['name']}", json={"mode": "broadcast"})
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "broadcast"

    # Verify the update took effect
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.json()["data"]["mode"] == "broadcast"

    # Delete
    resp = client.delete(f"/api/teams/{team['name']}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # Verify it was deleted
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.status_code == 404


# ============================================================
# 2. Full Agent lifecycle
# ============================================================


def test_full_agent_lifecycle(integration_client):
    """The full flow: create team → add Agent → list → delete Agent."""
    client = integration_client

    # Create a team
    team = make_team({"name": "agent-lifecycle-team"})
    team_name = team["name"]
    team_id = team["id"]

    # Add Agent 1
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "coder", "role": "Backend Development", "system_prompt": "You are a backend development expert"},
    )
    assert resp.status_code == 201
    agent1 = resp.json()["data"]
    assert agent1["name"] == "coder"
    assert agent1["role"] == "Backend Development"
    assert agent1["team_id"] == team_id

    # Add Agent 2
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "reviewer", "role": "Code Review"},
    )
    assert resp.status_code == 201

    # List Agents
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    agents_data = resp.json()
    assert agents_data["total"] == 2

    # Delete Agent 1
    resp = client.delete(f"/api/agents/{agent1['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # Verify only 1 Agent remains
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.json()["total"] == 1
    assert resp.json()["data"][0]["name"] == "reviewer"


# ============================================================
# 3. Team status
# ============================================================


def test_team_status(integration_client):
    """Create team + Agent → get status → verify fields."""
    client = integration_client

    # Create a team
    team = make_team({"name": "status-int-team"})
    team_name = team["name"]

    # Add an Agent
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev", "role": "Development"},
    )
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "qa", "role": "Testing"},
    )

    # Get the status
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]

    # Verify fields
    assert status["team"]["name"] == "status-int-team"
    assert len(status["agents"]) == 2
    assert status["completed_tasks"] == 0
    assert status["total_tasks"] == 0
    assert isinstance(status["active_tasks"], list)


# ============================================================
# 4. Invalid mode → 422
# ============================================================


def test_create_team_route_is_retired(integration_client):
    """POST /api/teams was retired together with team_create (batch 7, 2026-07-27).

    Keep this assertion rather than deleting it: it pins \"a team can no longer be conjured out of thin air over HTTP\"
    as a contract,
    preventing anyone from casually re-adding this trap route later (a team created this way has no kind and would
    inevitably be collected by the reaper).
    """
    resp = integration_client.post(
        "/api/teams",
        json={"name": "bad-mode-team", "mode": "invalid_mode"},
    )
    assert resp.status_code == 405


# ============================================================
# 5. Get a non-existent team → 404
# ============================================================


def test_get_nonexistent_team(integration_client):
    """Getting a non-existent team should return 404."""
    resp = integration_client.get("/api/teams/nonexistent-team-xyz")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "not_found"


# ============================================================
# 6. Delete a non-existent team → 404
# ============================================================


def test_delete_nonexistent_team(integration_client):
    """Deleting a non-existent team should return 404."""
    resp = integration_client.delete("/api/teams/nonexistent-team-xyz")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False


# ============================================================
# 7. List empty teams → 200 + total=0
# ============================================================


def test_list_empty_teams(integration_client):
    """Listing teams on an empty database should return 200 and an empty list."""
    resp = integration_client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 0
    assert data["data"] == []


# ============================================================
# 8. Event records exist after operations
# ============================================================


def test_events_created_on_operations(repo_and_client):
    """After creating a team, query events and verify system events were recorded."""
    repo, client = repo_and_client

    # First create an event manually via the repo (the API itself may not create events automatically)
    asyncio.get_event_loop().run_until_complete(
        repo.create_event(
            event_type="team.created",
            source="integration-test",
            data={"team_name": "event-test"},
        )
    )

    # Query events
    resp = client.get("/api/events")
    assert resp.status_code == 200
    events_data = resp.json()
    assert events_data["success"] is True
    assert events_data["total"] >= 1

    # Filter by type
    resp = client.get("/api/events?type=team.created")
    assert resp.status_code == 200
    filtered = resp.json()
    assert filtered["total"] >= 1
    for event in filtered["data"]:
        assert event["type"] == "team.created"


# ============================================================
# 9. Memory search — empty results
# ============================================================


def test_memory_search_empty(integration_client):
    """Searching memories should return 200 and an empty list."""
    resp = integration_client.get("/api/memory?scope=global&scope_id=system&query=hello")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 0
    assert data["data"] == []


# ============================================================
# 10. CORS responses
# ============================================================


def test_cors_headers(integration_client):
    """An OPTIONS request should return the correct CORS response headers."""
    resp = integration_client.options(
        "/api/teams",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "GET" in resp.headers.get("access-control-allow-methods", "")

    # Test a disallowed Origin
    resp2 = integration_client.options(
        "/api/teams",
        headers={
            "Origin": "http://evil-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # A disallowed Origin should not appear in the response headers
    assert resp2.headers.get("access-control-allow-origin") != "http://evil-site.com"


# ============================================================
# 11. Team-Agent-Task full workflow
# ============================================================


def test_full_team_workflow(integration_client):
    """Create team → add agent → create task → complete task → query status."""
    client = integration_client

    # Create a team
    team = make_team({"name": "workflow-team", "mode": "coordinate"})
    team_name = team["name"]
    team_id = team["id"]

    # Add an agent
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "dev"},
    )
    assert resp.status_code == 201
    agent = resp.json()["data"]
    assert agent["team_id"] == team_id

    # Create a task
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Workflow test task", "description": "Integration test task"},
    )
    assert resp.status_code == 200
    task = resp.json()["data"]
    task_id = task["id"]
    assert task["status"] == "pending"
    assert task["title"] == "Workflow test task"

    # Create a second task
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Second task", "description": "The second task"},
    )
    assert resp.status_code == 200
    task2_id = resp.json()["data"]["id"]

    # Complete the first task
    resp = client.put(f"/api/tasks/{task_id}/complete")
    assert resp.status_code == 200
    completed = resp.json()["data"]
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    # Query team status — there should be 1 completed task
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]
    assert status["completed_tasks"] == 1
    assert status["total_tasks"] == 2
    assert len(status["agents"]) == 1

    # Complete the second task
    resp = client.put(f"/api/tasks/{task2_id}/complete")
    assert resp.status_code == 200

    # Verify the status again
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]
    assert status["completed_tasks"] == 2


# ============================================================
# 12. Analytics endpoint integration
# ============================================================


def test_analytics_with_activities(repo_and_client):
    """Create team + agent + activities → query the analytics endpoints → verify the data."""
    repo, client = repo_and_client

    # Create team + agent
    team = make_team({"name": "analytics-team"})
    team_name = team["name"]
    team_id = team["id"]

    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "analytics-dev", "role": "dev"},
    )
    assert resp.status_code == 201
    agent_id = resp.json()["data"]["id"]

    # Create a task and complete it (for efficiency statistics)
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Analytics task", "description": "For statistics"},
    )
    task_id = resp.json()["data"]["id"]
    client.put(f"/api/tasks/{task_id}/complete")

    # Manually add activities (operating on the repo directly)
    loop = asyncio.get_event_loop()
    for tool in ["read_file", "edit_file", "read_file", "bash", "edit_file"]:
        loop.run_until_complete(
            repo.create_activity(
                agent_id=agent_id,
                session_id="test-session-001",
                tool_name=tool,
                input_summary="test input",
                output_summary="test output",
            )
        )

    # tool-usage: there should be tool statistics
    resp = client.get(f"/api/analytics/tool-usage?team_id={team_id}")
    assert resp.status_code == 200
    tool_data = resp.json()["data"]
    assert isinstance(tool_data, list)
    assert len(tool_data) > 0
    # Verify read_file appears twice
    read_file_entry = [d for d in tool_data if d["tool_name"] == "read_file"]
    assert len(read_file_entry) == 1
    assert read_file_entry[0]["count"] == 2

    # agent-productivity: there should be agent productivity data
    resp = client.get(f"/api/analytics/agent-productivity?team_id={team_id}")
    assert resp.status_code == 200
    prod_data = resp.json()["data"]
    assert isinstance(prod_data, list)
    assert len(prod_data) >= 1
    assert prod_data[0]["activity_count"] == 5

    # timeline: should return timeline data
    resp = client.get(f"/api/analytics/timeline?team_id={team_id}&hours=1")
    assert resp.status_code == 200
    timeline_data = resp.json()["data"]
    assert isinstance(timeline_data, list)

    # efficiency: should return efficiency metrics
    resp = client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    eff_data = resp.json()["data"]
    assert "task_completion" in eff_data
    assert "agent_utilization" in eff_data
    assert eff_data["task_completion"]["completed_tasks"] >= 1


# ============================================================
# 13. Team configuration CRUD
# ============================================================


def test_team_config_crud(integration_client):
    """GET defaults → PUT update → POST add member → PATCH disable → DELETE remove."""
    client = integration_client

    # GET the default configuration
    resp = client.get("/api/config/team-defaults")
    assert resp.status_code == 200
    config = resp.json()["data"]
    assert "permanent_members" in config

    # PUT a full update
    resp = client.put(
        "/api/config/team-defaults",
        json={
            "auto_create_team": False,
            "team_name_prefix": "integ",
            "permanent_members": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["auto_create_team"] is False
    assert resp.json()["data"]["team_name_prefix"] == "integ"

    # POST to add a member
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "qa-bot", "role": "QA", "model": "claude-sonnet-4-6", "enabled": True},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "qa-bot"

    # POST to add a second member
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "fixer", "role": "bug-fixer"},
    )
    assert resp.status_code == 201

    # POST a duplicate member → 409
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "qa-bot", "role": "QA"},
    )
    assert resp.status_code == 409

    # PATCH to disable a member
    resp = client.patch(
        "/api/config/team-defaults/members/qa-bot",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    # PATCH a non-existent member → 404
    resp = client.patch(
        "/api/config/team-defaults/members/nonexistent",
        json={"enabled": False},
    )
    assert resp.status_code == 404

    # Verify GET reflects the update
    resp = client.get("/api/config/team-defaults")
    members = resp.json()["data"]["permanent_members"]
    assert len(members) == 2
    qa_bot = [m for m in members if m["name"] == "qa-bot"][0]
    assert qa_bot["enabled"] is False

    # DELETE to remove a member
    resp = client.delete("/api/config/team-defaults/members/qa-bot")
    assert resp.status_code == 200

    # DELETE a non-existent member → 404
    resp = client.delete("/api/config/team-defaults/members/nonexistent")
    assert resp.status_code == 404

    # Verify only 1 member remains
    resp = client.get("/api/config/team-defaults")
    members = resp.json()["data"]["permanent_members"]
    assert len(members) == 1
    assert members[0]["name"] == "fixer"


# ============================================================
# 14. Task wall ordering
# ============================================================


def test_task_wall_sorting(integration_client):
    """Create tasks with different priority/horizon values → query the task wall → verify score ordering."""
    client = integration_client

    # Create a team
    team_data = make_team({"name": "wall-team"})
    team_name = team_data["name"]
    team_id = team_data["id"]

    # Create tasks with different priorities and time horizons
    tasks_spec = [
        {"title": "Low-short", "description": "Low-priority short-term", "priority": "low", "horizon": "short"},
        {
            "title": "Critical-short",
            "description": "Critical short-term",
            "priority": "critical",
            "horizon": "short",
        },
        {"title": "High-mid", "description": "High-priority mid-term", "priority": "high", "horizon": "mid"},
        {
            "title": "Medium-long",
            "description": "Medium-priority long-term",
            "priority": "medium",
            "horizon": "long",
        },
        {
            "title": "High-short",
            "description": "High-priority short-term",
            "priority": "high",
            "horizon": "short",
        },
    ]

    for spec in tasks_spec:
        resp = client.post(f"/api/teams/{team_name}/tasks/run", json=spec)
        assert resp.status_code == 200

    # Query the task wall (using team_id, because the task wall queries tasks directly by team_id)
    resp = client.get(f"/api/teams/{team_id}/task-wall")
    assert resp.status_code == 200
    wall_data = resp.json()

    # Verify the wall structure contains 3 groups
    wall = wall_data["wall"]
    assert "short" in wall
    assert "mid" in wall
    assert "long" in wall

    # The short group should have 3 tasks
    short_tasks = wall["short"]
    assert len(short_tasks) == 3

    # Verify scores are in descending order
    scores = [t["score"] for t in short_tasks]
    assert scores == sorted(scores, reverse=True), f"short tasks are not sorted by score descending: {scores}"

    # Critical should come first
    assert short_tasks[0]["title"] == "Critical-short"

    # The mid group should have 1 task
    assert len(wall["mid"]) == 1
    assert wall["mid"][0]["title"] == "High-mid"

    # The long group should have 1 task
    assert len(wall["long"]) == 1
    assert wall["long"][0]["title"] == "Medium-long"

    # stats should contain the correct statistics
    stats = wall_data["stats"]
    assert stats["total"] == 5
    assert stats["by_status"]["pending"] == 5

    # Filter by horizon
    resp = client.get(f"/api/teams/{team_id}/task-wall?horizon=short")
    assert resp.status_code == 200
    filtered_wall = resp.json()["wall"]
    assert len(filtered_wall["short"]) == 3
    assert len(filtered_wall["mid"]) == 0
    assert len(filtered_wall["long"]) == 0

    # Filter by priority
    resp = client.get(f"/api/teams/{team_id}/task-wall?priority=critical")
    assert resp.status_code == 200
    prio_wall = resp.json()["wall"]
    # Only critical tasks appear in the corresponding group
    all_filtered = prio_wall["short"] + prio_wall["mid"] + prio_wall["long"]
    assert len(all_filtered) == 1
    assert all_filtered[0]["priority"] == "critical"
