"""AI Team OS — Agent node implementation.

Each Agent is a node in LangGraph that receives state, calls LLM, and returns output.
The factory function create_agent_node creates a corresponding node function for each Agent.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from aiteam.orchestrator.llm_factory import resolve_llm
from aiteam.types import Agent


def create_agent_node(
    agent_config: Agent,
    memory_store: Any | None = None,
) -> Callable[..., Coroutine[Any, Any, dict]]:
    """Factory function to create a LangGraph node function for a given Agent.

    Args:
        agent_config: Agent config (contains name, role, system_prompt, model, etc.).
        memory_store: Optional MemoryStore instance for injecting memory context.

    Returns:
        Async node function with signature (state, config) -> dict.
    """

    async def agent_node(state: dict, config: RunnableConfig) -> dict:
        """Agent executes its assigned subtask.

        Extracts its subtask from leader_plan, combines with memory context to call LLM,
        and writes output to agent_outputs.

        Args:
            state: LangGraph state dictionary.
            config: Runtime configuration.

        Returns:
            State update dict containing agent_outputs and messages.
        """
        configurable = config.get("configurable", {})
        llm_model = configurable.get("llm_model", agent_config.model)

        task = state.get("current_task", "")
        leader_plan = state.get("leader_plan", "")

        # Build system prompt
        base_prompt = agent_config.system_prompt or f"You are a {agent_config.role}."
        system_parts = [base_prompt]

        # Inject memory context (if memory_store is available)
        if memory_store is not None:
            try:
                memory_context = await memory_store.get_context(
                    agent_id=agent_config.id,
                    task=task,
                )
                if memory_context:
                    system_parts.append(f"\n## Relevant Memories\n{memory_context}")
            except Exception:
                # Silently skip when memory is unavailable
                pass

        system_content = "\n".join(system_parts)

        user_content = (
            f"## Team Task\n{task}\n\n"
            f"## The Leader's Execution Plan\n{leader_plan}\n\n"
            f"Please complete your part according to the subtask assigned to you in the plan "
            f"({agent_config.name}, {agent_config.role}). "
            f"Output the work product directly."
        )

        llm = resolve_llm(configurable, llm_model)
        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=user_content),
        ]
        response = await llm.ainvoke(messages)

        # Merge into existing agent_outputs
        existing_outputs = dict(state.get("agent_outputs", {}))
        existing_outputs[agent_config.name] = response.content

        return {
            "agent_outputs": existing_outputs,
            "messages": [response],
        }

    # Set function name for debugging
    agent_node.__name__ = f"agent_{agent_config.name}"
    agent_node.__qualname__ = f"agent_{agent_config.name}"

    return agent_node
