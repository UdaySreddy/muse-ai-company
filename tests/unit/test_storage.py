"""AI Team OS — StorageRepository unit tests."""

from __future__ import annotations

import pytest

from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

# ================================================================
# Teams
# ================================================================


async def test_create_and_get_team(db_repository: StorageRepository) -> None:
    """Create a team and get it by ID and by name."""
    team = await db_repository.create_team("alpha", "coordinate", {"env": "test"})
    assert team.name == "alpha"
    assert team.mode.value == "coordinate"
    assert team.config == {"env": "test"}

    # Get by ID
    by_id = await db_repository.get_team(team.id)
    assert by_id is not None
    assert by_id.id == team.id
    assert by_id.name == "alpha"

    # Get by name
    by_name = await db_repository.get_team_by_name("alpha")
    assert by_name is not None
    assert by_name.id == team.id

    # A nonexistent team returns None
    assert await db_repository.get_team("nonexistent") is None
    assert await db_repository.get_team_by_name("nonexistent") is None


async def test_get_or_create_team_found_existing(db_repository: StorageRepository) -> None:
    """A team with the same name exists → return the existing row with created=False, do not create a new one."""
    first = await db_repository.create_team("dup-a", "coordinate", {"k": 1})
    team, created = await db_repository.get_or_create_team("dup-a", "coordinate", {"k": 2})
    assert created is False
    assert team.id == first.id
    assert team.config == {"k": 1}  # The existing one was not overwritten
    assert len([t for t in await db_repository.list_teams() if t.name == "dup-a"]) == 1


async def test_get_or_create_team_creates_new(db_repository: StorageRepository) -> None:
    """No team with the same name → create with created=True, exactly one row."""
    team, created = await db_repository.get_or_create_team("fresh-a", "coordinate")
    assert created is True
    assert team.name == "fresh-a"
    assert (await db_repository.get_team_by_name("fresh-a")).id == team.id


async def test_get_or_create_team_concurrent_single_row(tmp_path) -> None:
    """Concurrent find-or-create of the same-named team: no exception, exactly one row, all return the same id, exactly
    one created.

    Use a **file database** (not an in-memory StaticPool) to reproduce production's independent-connection topology —
    each request takes an independent connection
    from the pool; after the loser hits UNIQUE, re-fetching can see the winner's committed row. In-memory
    single-connection
    StaticPool concurrent transaction snapshots are mutually invisible — not the real scenario.
    """
    import asyncio

    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path}/race.db")
    await repo.init_db()

    results = await asyncio.gather(
        *(repo.get_or_create_team("race-a", "coordinate") for _ in range(5))
    )
    ids = {t.id for t, _ in results}
    assert len(ids) == 1, "Concurrent creation of the same-named team should converge to a single row"
    assert sum(1 for _, created in results if created) == 1, "Only one truly created"
    rows = [t for t in await repo.list_teams() if t.name == "race-a"]
    assert len(rows) == 1


async def test_get_or_create_team_swallows_integrity_race(
    db_repository: StorageRepository, monkeypatch
) -> None:
    """Deterministically cover the except branch: get misses first (simulating concurrency) → create hits UNIQUE →
    swallow the error and re-fetch the existing row."""
    seeded = await db_repository.create_team("forced-a", "coordinate")

    orig_get = db_repository.get_team_by_name
    calls = {"n": 0}

    async def flaky(name: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # Simulate "not found" under concurrency, forcing get_or_create into the create branch
        return await orig_get(name)

    monkeypatch.setattr(db_repository, "get_team_by_name", flaky)
    team, created = await db_repository.get_or_create_team("forced-a", "coordinate")
    assert created is False  # Re-fetched after hitting UNIQUE, not newly created
    assert team.id == seeded.id
    assert calls["n"] == 2  # First miss + re-fetch inside except


async def test_list_teams(db_repository: StorageRepository) -> None:
    """List all teams."""
    await db_repository.create_team("team-a", "coordinate")
    await db_repository.create_team("team-b", "broadcast")

    teams = await db_repository.list_teams()
    assert len(teams) == 2
    names = {t.name for t in teams}
    assert names == {"team-a", "team-b"}


async def test_update_team(db_repository: StorageRepository) -> None:
    """Update team information."""
    team = await db_repository.create_team("updatable", "coordinate")

    updated = await db_repository.update_team(team.id, mode="broadcast")
    assert updated.mode.value == "broadcast"

    # Verify persistence
    fetched = await db_repository.get_team(team.id)
    assert fetched is not None
    assert fetched.mode.value == "broadcast"

    # Updating a nonexistent team should raise
    with pytest.raises(ValueError, match="does not exist"):
        await db_repository.update_team("nonexistent", mode="route")


async def test_delete_team(db_repository: StorageRepository) -> None:
    """Delete a team."""
    team = await db_repository.create_team("deletable", "coordinate")
    assert await db_repository.delete_team(team.id) is True

    # Confirm deleted
    assert await db_repository.get_team(team.id) is None

    # Deleting a nonexistent team returns False
    assert await db_repository.delete_team("nonexistent") is False


# ================================================================
# Agents
# ================================================================


async def test_create_and_get_agent(db_repository: StorageRepository) -> None:
    """Create an Agent and get it."""
    team = await db_repository.create_team("dev-team", "coordinate")
    agent = await db_repository.create_agent(
        team.id, "dev-1", "Backend Development", system_prompt="You are a backend development engineer"
    )
    assert agent.name == "dev-1"
    assert agent.role == "Backend Development"
    assert agent.system_prompt == "You are a backend development engineer"
    assert agent.team_id == team.id

    fetched = await db_repository.get_agent(agent.id)
    assert fetched is not None
    assert fetched.name == "dev-1"

    # A nonexistent Agent returns None
    assert await db_repository.get_agent("nonexistent") is None


async def test_list_agents_by_team(db_repository: StorageRepository) -> None:
    """List Agents by team, ensuring team isolation."""
    team_a = await db_repository.create_team("team-a", "coordinate")
    team_b = await db_repository.create_team("team-b", "coordinate")

    await db_repository.create_agent(team_a.id, "a1", "Development")
    await db_repository.create_agent(team_a.id, "a2", "Testing")
    await db_repository.create_agent(team_b.id, "b1", "Design")

    agents_a = await db_repository.list_agents(team_a.id)
    assert len(agents_a) == 2
    assert {a.name for a in agents_a} == {"a1", "a2"}

    agents_b = await db_repository.list_agents(team_b.id)
    assert len(agents_b) == 1
    assert agents_b[0].name == "b1"


# ================================================================
# Tasks
# ================================================================


async def test_create_and_get_task(db_repository: StorageRepository) -> None:
    """Create a task and get it."""
    team = await db_repository.create_team("task-team", "coordinate")
    task = await db_repository.create_task(team.id, "Implement login", "Implement the user login feature")
    assert task.title == "Implement login"
    assert task.description == "Implement the user login feature"
    assert task.status == TaskStatus.PENDING

    fetched = await db_repository.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "Implement login"

    # A nonexistent task returns None
    assert await db_repository.get_task("nonexistent") is None


async def test_list_tasks_filter_status(db_repository: StorageRepository) -> None:
    """List tasks and filter by status."""
    team = await db_repository.create_team("filter-team", "coordinate")

    task1 = await db_repository.create_task(team.id, "Task 1")
    task2 = await db_repository.create_task(team.id, "Task 2")
    task3 = await db_repository.create_task(team.id, "Task 3")

    # Mark task2 as running
    await db_repository.update_task(task2.id, status="running")
    # Mark task3 as completed
    await db_repository.update_task(task3.id, status="completed")

    # No filter — return all
    all_tasks = await db_repository.list_tasks(team.id)
    assert len(all_tasks) == 3

    # Filter by status
    pending = await db_repository.list_tasks(team.id, status=TaskStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].id == task1.id

    running = await db_repository.list_tasks(team.id, status=TaskStatus.RUNNING)
    assert len(running) == 1
    assert running[0].id == task2.id

    completed = await db_repository.list_tasks(team.id, status=TaskStatus.COMPLETED)
    assert len(completed) == 1
    assert completed[0].id == task3.id


# ================================================================
# Events
# ================================================================


async def test_create_and_list_events(db_repository: StorageRepository) -> None:
    """Create events and list them by condition."""
    await db_repository.create_event("team.created", "system", {"team": "alpha"})
    await db_repository.create_event("agent.created", "system", {"agent": "dev-1"})
    await db_repository.create_event("team.created", "cli", {"team": "beta"})

    # List all
    all_events = await db_repository.list_events()
    assert len(all_events) == 3

    # Filter by type
    team_events = await db_repository.list_events(event_type="team.created")
    assert len(team_events) == 2

    # Filter by source
    cli_events = await db_repository.list_events(source="cli")
    assert len(cli_events) == 1
    assert cli_events[0].data["team"] == "beta"

    # Filter by both type and source
    filtered = await db_repository.list_events(event_type="team.created", source="system")
    assert len(filtered) == 1

    # Limit the count
    limited = await db_repository.list_events(limit=1)
    assert len(limited) == 1


# ================================================================
# Memories
# ================================================================


async def test_create_and_search_memories(db_repository: StorageRepository) -> None:
    """Create memories and search."""
    team = await db_repository.create_team("mem-team", "coordinate")

    m1 = await db_repository.create_memory(
        "team", team.id, "The project is developed with Python and FastAPI", {"tag": "tech"}
    )
    await db_repository.create_memory(
        "team", team.id, "The team has 3 backend and 2 frontend members", {"tag": "org"}
    )
    await db_repository.create_memory("agent", "agent-001", "This Agent is good at data analysis")

    # Get a single memory
    fetched = await db_repository.get_memory(m1.id)
    assert fetched is not None
    assert fetched.content == "The project is developed with Python and FastAPI"
    assert fetched.metadata == {"tag": "tech"}

    # List memories in the specified scope
    team_mems = await db_repository.list_memories("team", team.id)
    assert len(team_mems) == 2

    agent_mems = await db_repository.list_memories("agent", "agent-001")
    assert len(agent_mems) == 1

    # Keyword search
    python_results = await db_repository.search_memories("team", team.id, "Python")
    assert len(python_results) == 1
    assert "Python" in python_results[0].content

    # A keyword that finds nothing
    empty = await db_repository.search_memories("team", team.id, "Java")
    assert len(empty) == 0

    # Cross-scope searches find nothing
    cross_scope = await db_repository.search_memories("agent", "agent-001", "Python")
    assert len(cross_scope) == 0

    # Delete the memory
    assert await db_repository.delete_memory(m1.id) is True
    assert await db_repository.get_memory(m1.id) is None
    assert await db_repository.delete_memory("nonexistent") is False
