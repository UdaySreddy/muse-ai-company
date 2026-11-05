"""AI Team OS — Human-in-the-Loop approval node unit tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("langgraph", reason="requires ai-team-os[langgraph] extra")

from langgraph.types import Command

from aiteam.orchestrator.graphs.coordinate import build_coordinate_graph
from aiteam.orchestrator.nodes.approval_node import approval_node
from aiteam.types import Agent

# ================================================================
# Fixtures
# ================================================================


@pytest.fixture()
def sample_agents() -> list[Agent]:
    """Create the test Agent list."""
    return [
        Agent(team_id="t1", name="researcher", role="Researcher"),
        Agent(team_id="t1", name="developer", role="Developer"),
    ]


# ================================================================
# approval_node tests
# ================================================================


@pytest.mark.asyncio()
async def test_approval_node_approved() -> None:
    """When approval passes, returns the approved status."""
    state = {
        "leader_plan": "1. researcher researches 2. developer implements",
    }

    # Mock interrupt returns approval granted
    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": True}
        result = await approval_node(state, config={})

    assert isinstance(result, dict)
    assert result["approval_status"] == "approved"


@pytest.mark.asyncio()
async def test_approval_node_rejected() -> None:
    """When approval is rejected, returns a Command jumping to END."""
    state = {
        "leader_plan": "1. researcher researches 2. developer implements",
    }

    # Mock interrupt returns approval rejected
    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": False, "reason": "The plan is unreasonable"}
        result = await approval_node(state, config={})

    assert isinstance(result, Command)
    assert result.goto == "__end__"
    assert result.update["approval_status"] == "rejected"
    assert "The plan is unreasonable" in result.update["final_result"]


@pytest.mark.asyncio()
async def test_approval_node_rejected_no_reason() -> None:
    """When approval is rejected without a reason, a default reason is used."""
    state = {"leader_plan": "Test plan"}

    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": False}
        result = await approval_node(state, config={})

    assert isinstance(result, Command)
    assert "No reason given" in result.update["final_result"]


@pytest.mark.asyncio()
async def test_approval_interrupt_value() -> None:
    """Verify the data format passed to interrupt is correct."""
    state = {
        "leader_plan": "Test execution plan content",
    }

    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": True}
        await approval_node(state, config={})

    # Verify interrupt was called with the correct data
    mock_interrupt.assert_called_once()
    interrupt_data = mock_interrupt.call_args[0][0]
    assert interrupt_data["type"] == "approval_request"
    assert interrupt_data["plan"] == "Test execution plan content"
    assert interrupt_data["message"] == "Please approve the following execution plan"


# ================================================================
# Coordinate graph structure tests (with approval)
# ================================================================


def test_coordinate_graph_with_approval(sample_agents: list[Agent]) -> None:
    """A graph with approval should contain an approval node."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=True,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


def test_coordinate_graph_without_approval(sample_agents: list[Agent]) -> None:
    """A graph without approval is unchanged in structure, with no approval node."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=False,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" not in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


def test_coordinate_graph_with_approval_no_agents() -> None:
    """With approval but no Agents, approval connects directly to leader_synthesize."""
    graph = build_coordinate_graph(
        agents=[],
        memory_store=None,
        require_approval=True,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" in node_names
    assert "leader_synthesize" in node_names

    # No Agent nodes
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


def test_coordinate_graph_with_approval_compiles(sample_agents: list[Agent]) -> None:
    """A graph with approval can be compiled successfully."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=True,
    )
    compiled = graph.compile()
    assert hasattr(compiled, "ainvoke")


def test_coordinate_graph_default_no_approval(sample_agents: list[Agent]) -> None:
    """Approval is not enabled by default."""
    graph = build_coordinate_graph(agents=sample_agents, memory_store=None)
    node_names = set(graph.nodes.keys())
    assert "approval" not in node_names
