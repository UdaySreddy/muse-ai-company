"""AI Team OS — Orchestrator unit tests."""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="requires ai-team-os[langgraph] extra")
pytest.importorskip("langgraph", reason="requires ai-team-os[langgraph] extra")

from aiteam.orchestrator.graph_compiler import compile_graph
from aiteam.orchestrator.graphs.coordinate import build_coordinate_graph
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    Agent,
    OrchestrationMode,
    Team,
)

# ================================================================
# Fixtures
# ================================================================


@pytest.fixture()
def manager(db_repository: StorageRepository) -> TeamManager:
    """Create a TeamManager instance (no memory)."""
    return TeamManager(repository=db_repository, memory=None)


# ================================================================
# Team management
# ================================================================


async def test_create_team(manager: TeamManager) -> None:
    """Create a team and verify its fields."""
    team = await manager.create_team("dev-team", mode="coordinate")
    assert team.name == "dev-team"
    assert team.mode == OrchestrationMode.COORDINATE
    assert team.id

    # Get by name
    fetched = await manager.get_team("dev-team")
    assert fetched.id == team.id


async def test_create_team_invalid_mode(manager: TeamManager) -> None:
    """An invalid orchestration mode should raise ValueError."""
    with pytest.raises(ValueError):
        await manager.create_team("bad-team", mode="invalid_mode")


async def test_list_teams(manager: TeamManager) -> None:
    """List all teams."""
    await manager.create_team("team-a")
    await manager.create_team("team-b")
    teams = await manager.list_teams()
    names = {t.name for t in teams}
    assert "team-a" in names
    assert "team-b" in names


async def test_delete_team(manager: TeamManager) -> None:
    """Delete a team."""
    await manager.create_team("to-delete")
    result = await manager.delete_team("to-delete")
    assert result is True

    with pytest.raises(ValueError, match="does not exist"):
        await manager.get_team("to-delete")


async def test_get_team_not_found(manager: TeamManager) -> None:
    """Getting a nonexistent team should raise ValueError."""
    with pytest.raises(ValueError, match="does not exist"):
        await manager.get_team("nonexistent")


async def test_set_mode(manager: TeamManager) -> None:
    """Switch a team's orchestration mode."""
    await manager.create_team("mode-team", mode="coordinate")
    updated = await manager.set_mode("mode-team", "broadcast")
    assert updated.mode == "broadcast" or updated.mode == OrchestrationMode.BROADCAST


async def test_set_mode_invalid(manager: TeamManager) -> None:
    """Setting an invalid orchestration mode should raise ValueError."""
    await manager.create_team("mode-team2")
    with pytest.raises(ValueError):
        await manager.set_mode("mode-team2", "nonexistent_mode")


# ================================================================
# Agent management
# ================================================================


async def test_add_agent(manager: TeamManager) -> None:
    """Add an Agent to a team and verify its fields."""
    await manager.create_team("agent-team")
    agent = await manager.add_agent(
        team_name="agent-team",
        name="dev-1",
        role="Backend Development",
        system_prompt="You are a backend development engineer.",
        model="claude-opus-4-7",
    )
    assert agent.name == "dev-1"
    assert agent.role == "Backend Development"
    assert agent.system_prompt == "You are a backend development engineer."
    assert agent.model == "claude-opus-4-7"


async def test_list_agents(manager: TeamManager) -> None:
    """List all Agents in a team."""
    await manager.create_team("list-team")
    await manager.add_agent("list-team", "agent-a", "Researcher")
    await manager.add_agent("list-team", "agent-b", "Developer")

    agents = await manager.list_agents("list-team")
    assert len(agents) == 2
    names = {a.name for a in agents}
    assert names == {"agent-a", "agent-b"}


async def test_remove_agent(manager: TeamManager) -> None:
    """Remove an Agent from a team."""
    await manager.create_team("remove-team")
    await manager.add_agent("remove-team", "to-remove", "Test Role")

    result = await manager.remove_agent("remove-team", "to-remove")
    assert result is True

    agents = await manager.list_agents("remove-team")
    assert len(agents) == 0


async def test_remove_agent_not_found(manager: TeamManager) -> None:
    """Removing a nonexistent Agent should raise ValueError."""
    await manager.create_team("remove-team2")
    with pytest.raises(ValueError, match="does not exist"):
        await manager.remove_agent("remove-team2", "ghost-agent")


# ================================================================
# Status queries
# ================================================================


async def test_get_status(manager: TeamManager) -> None:
    """Get a team status summary."""
    await manager.create_team("status-team")
    await manager.add_agent("status-team", "worker-1", "Developer")
    await manager.add_agent("status-team", "worker-2", "Tester")

    status = await manager.get_status("status-team")
    assert status.team.name == "status-team"
    assert len(status.agents) == 2
    assert status.total_tasks == 0
    assert status.completed_tasks == 0


async def test_get_status_no_team(manager: TeamManager) -> None:
    """Getting status with no team should raise ValueError."""
    with pytest.raises(ValueError, match="No team available"):
        await manager.get_status(None)


# ================================================================
# Graph compilation
# ================================================================


async def test_compile_coordinate_graph(manager: TeamManager) -> None:
    """Compile the Coordinate graph and verify the graph structure."""
    agents = [
        Agent(team_id="t1", name="researcher", role="Researcher"),
        Agent(team_id="t1", name="developer", role="Developer"),
    ]
    graph = build_coordinate_graph(agents=agents, memory_store=None)

    # Verify the nodes exist
    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


async def test_compile_coordinate_graph_no_agents() -> None:
    """With no Agents, the graph should connect leader_plan directly to leader_synthesize."""
    graph = build_coordinate_graph(agents=[], memory_store=None)
    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "leader_synthesize" in node_names
    # No Agent nodes
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


async def test_compile_coordinate_graph_via_compiler() -> None:
    """Compile Coordinate mode via compile_graph."""
    team = Team(name="test-team", mode=OrchestrationMode.COORDINATE)
    agents = [Agent(team_id=team.id, name="worker", role="Engineer")]
    compiled = compile_graph(team=team, agents=agents)
    # compiled should be callable (CompiledGraph)
    assert hasattr(compiled, "ainvoke")


async def test_compile_unsupported_mode() -> None:
    """An unsupported orchestration mode should raise NotImplementedError."""
    agents = [Agent(team_id="t1", name="worker", role="Engineer")]

    # Broadcast was implemented in M2 and should compile normally
    team_broadcast = Team(name="t", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team_broadcast, agents=agents)
    assert hasattr(compiled, "ainvoke")

    team_route = Team(name="t", mode=OrchestrationMode.ROUTE)
    with pytest.raises(NotImplementedError, match="Route"):
        compile_graph(team=team_route, agents=agents)

    team_meet = Team(name="t", mode=OrchestrationMode.MEET)
    with pytest.raises(NotImplementedError, match="Meet"):
        compile_graph(team=team_meet, agents=agents)
