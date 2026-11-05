"""AI Team OS — Reducer node implementation.

Reducer collects all Agent parallel outputs and uses LLM to intelligently merge
them into a final result. Used in Broadcast orchestration mode as the synthesis node.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from aiteam.orchestrator.llm_factory import resolve_llm


async def reducer_node(state: dict, config: RunnableConfig) -> dict:
    """Collect all Agent outputs and intelligently merge into final result.

    Reads agent_outputs and original task, calls LLM to synthesize all Agent
    parallel outputs into the final final_result.

    Args:
        state: LangGraph state dictionary.
        config: Runtime configuration.

    Returns:
        State update dict containing final_result and messages.
    """
    configurable = config.get("configurable", {})
    llm_model = configurable.get("llm_model", "claude-opus-4-8")

    task = state.get("current_task", "")
    agent_outputs = state.get("agent_outputs", {})

    # Build output summary for each Agent
    outputs_text = []
    for agent_name, output in agent_outputs.items():
        outputs_text.append(f"### {agent_name}'s output:\n{output}")
    all_outputs = "\n\n".join(outputs_text) if outputs_text else "No Agent output"

    system_prompt = (
        "You are a result merger (Reducer), responsible for merging the results of multiple Agents' parallel execution "
        "into one complete final output.\n"
        "All Agents received the same task and each completed it independently; now you need to:\n"
        "1. Identify the common points and unique contributions in each Agent's output\n"
        "2. Remove duplicated content\n"
        "3. Integrate different perspectives and insights\n"
        "4. Generate a synthesized, comprehensive, coherent final result\n\n"
        "Output the merged final result directly, without extra explanation."
    )

    user_content = f"## Original Task\n{task}\n\n## The Agents' Parallel Outputs\n{all_outputs}"

    llm = resolve_llm(configurable, llm_model)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    response = await llm.ainvoke(messages)

    return {
        "final_result": response.content,
        "messages": [response],
    }
