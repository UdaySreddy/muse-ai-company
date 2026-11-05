"""AI Team OS — Broadcast orchestration mode unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("langchain_core", reason="requires ai-team-os[langgraph] extra")
pytest.importorskip("langgraph", reason="requires ai-team-os[langgraph] extra")

from langchain_core.messages import AIMessage

from aiteam.orchestrator.graph_compiler import compile_graph
from aiteam.orchestrator.graphs.broadcast import (
    _broadcast_node,
    build_broadcast_graph,
)
from aiteam.orchestrator.nodes.reducer_node import reducer_node
from aiteam.types import Agent, OrchestrationMode, Team

# ================================================================
# Fixtures
# ================================================================


@pytest.fixture()
def sample_agents() -> list[Agent]:
    """Create the test Agent list."""
    return [
        Agent(team_id="t1", name="researcher", role="Researcher"),
        Agent(team_id="t1", name="developer", role="Developer"),
        Agent(team_id="t1", name="reviewer", role="Reviewer"),
    ]


@pytest.fixture()
def single_agent() -> list[Agent]:
    """Create a single-Agent list."""
    return [Agent(team_id="t1", name="worker", role="Engineer")]


# ================================================================
# Graph structure tests
# ================================================================


def test_build_broadcast_graph_structure(sample_agents: list[Agent]) -> None:
    """Verify the structure of the Broadcast graph's nodes and edges."""
    graph = build_broadcast_graph(agents=sample_agents, memory_store=None)

    # Verify all nodes exist
    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "agent_reviewer" in node_names
    assert "reducer_node" in node_names


def test_build_broadcast_graph_no_agents() -> None:
    """With no Agents, the graph should go directly from broadcast_node to reducer_node."""
    graph = build_broadcast_graph(agents=[], memory_store=None)

    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "reducer_node" in node_names

    # No Agent nodes
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


def test_build_broadcast_graph_single_agent(single_agent: list[Agent]) -> None:
    """With a single Agent, the graph structure is correct."""
    graph = build_broadcast_graph(agents=single_agent, memory_store=None)

    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "agent_worker" in node_names
    assert "reducer_node" in node_names

    # Only one Agent node
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 1


def test_build_broadcast_graph_compiles(sample_agents: list[Agent]) -> None:
    """Verify the Broadcast graph can be compiled successfully."""
    graph = build_broadcast_graph(agents=sample_agents, memory_store=None)
    compiled = graph.compile()
    assert hasattr(compiled, "ainvoke")


# ================================================================
# broadcast_node tests
# ================================================================


def test_broadcast_node() -> None:
    """Verify broadcast_node correctly initializes agent_outputs."""
    state = {
        "team_id": "t1",
        "current_task": "Analyze market data",
        "messages": [],
        "agent_outputs": {"old_key": "old_value"},
        "final_result": None,
    }

    result = _broadcast_node(state)

    # Should initialize to an empty dict (clearing previous outputs)
    assert result["agent_outputs"] == {}


def test_broadcast_node_preserves_task() -> None:
    """Verify broadcast_node does not modify current_task."""
    state = {
        "current_task": "Test task",
        "agent_outputs": {},
    }

    result = _broadcast_node(state)

    # Should not contain an update to current_task
    assert "current_task" not in result


# ================================================================
# reducer_node tests
# ================================================================


@pytest.mark.asyncio()
async def test_reducer_node() -> None:
    """Verify reducer_node correctly calls the LLM and merges the results."""
    mock_response = AIMessage(content="The merged final result")

    state = {
        "current_task": "Analyze market trends",
        "agent_outputs": {
            "researcher": "Market research results...",
            "developer": "Technical analysis results...",
        },
    }

    # Standalone injection: the node resolves its LLM through
    # llm_factory.resolve_llm, so tests inject a mock via configurable["llm"]
    # instead of patching a hard-coded ChatAnthropic symbol.
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response
    config = {"configurable": {"llm_model": "claude-opus-4-7", "llm": mock_llm}}

    result = await reducer_node(state, config)

    assert result["final_result"] == "The merged final result"
    assert len(result["messages"]) == 1
    assert result["messages"][0] == mock_response

    # Verify the LLM call parameters contained all Agent outputs
    call_args = mock_llm.ainvoke.call_args[0][0]
    user_msg = call_args[1].content
    assert "researcher" in user_msg
    assert "developer" in user_msg


@pytest.mark.asyncio()
async def test_reducer_node_no_outputs() -> None:
    """Verify reducer_node's behavior when there are no Agent outputs."""
    mock_response = AIMessage(content="No content to merge")

    state = {
        "current_task": "Test task",
        "agent_outputs": {},
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response
    config = {"configurable": {"llm": mock_llm}}

    result = await reducer_node(state, config)

    assert result["final_result"] == "No content to merge"

    # Verify the prompt contains a no-output hint
    call_args = mock_llm.ainvoke.call_args[0][0]
    user_msg = call_args[1].content
    assert "No Agent output" in user_msg


# ================================================================
# compile_graph integration tests
# ================================================================


def test_compile_graph_broadcast_mode(sample_agents: list[Agent]) -> None:
    """Verify the compiler correctly selects Broadcast mode."""
    team = Team(name="broadcast-team", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team, agents=sample_agents)

    # The compiled output should be callable (a CompiledGraph)
    assert hasattr(compiled, "ainvoke")


def test_compile_graph_broadcast_no_agents() -> None:
    """Verify the compiler can compile a Broadcast graph even with no Agents."""
    team = Team(name="empty-team", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team, agents=[])
    assert hasattr(compiled, "ainvoke")


def test_compile_graph_coordinate_still_works() -> None:
    """Verify Coordinate mode still works normally after Broadcast was added."""
    team = Team(name="coord-team", mode=OrchestrationMode.COORDINATE)
    agents = [Agent(team_id=team.id, name="worker", role="Engineer")]
    compiled = compile_graph(team=team, agents=agents)
    assert hasattr(compiled, "ainvoke")
