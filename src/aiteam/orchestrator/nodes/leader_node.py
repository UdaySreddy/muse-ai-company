"""AI Team OS — Leader node implementation.

Leader analyzes tasks, creates work distribution plans, and synthesizes Agent outputs
into final results.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from aiteam.orchestrator.llm_factory import resolve_llm


async def leader_plan_node(state: dict, config: RunnableConfig) -> dict:
    """Leader analyzes the task and creates an execution plan.

    Reads current_task and agents config, calls LLM to generate a work distribution plan,
    and writes it to the leader_plan field.

    Args:
        state: LangGraph state dictionary.
        config: Runtime config containing configurable.agents, configurable.llm_model.

    Returns:
        State update dict containing leader_plan and messages.
    """
    configurable = config.get("configurable", {})
    agents = configurable.get("agents", [])
    llm_model = configurable.get("llm_model", "claude-opus-4-8")

    task = state.get("current_task", "")

    # Build Agent info descriptions
    agent_descriptions = []
    for agent in agents:
        desc = f"- {agent.name} (role: {agent.role})"
        if agent.system_prompt:
            desc += f" — {agent.system_prompt[:100]}"
        agent_descriptions.append(desc)
    agents_info = "\n".join(agent_descriptions) if agent_descriptions else "(No available agents)"

    system_prompt = (
        "You are a team's Leader, responsible for analyzing the task and making an execution plan.\n"
        "You need to decompose the task into subtasks and assign them to suitable team members.\n\n"
        "Your team members:\n"
        f"{agents_info}\n\n"
        "Please output a clear execution plan in the following format:\n"
        "1. Assign a concrete subtask to each Agent\n"
        "2. State each subtask's goal and requirements\n"
        "3. If some Agents do not need to participate, skip them\n\n"
        "Output the plan content directly, without extra explanation."
    )

    llm = resolve_llm(configurable, llm_model)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Task: {task}"),
    ]
    response = await llm.ainvoke(messages)

    return {
        "leader_plan": response.content,
        "messages": [response],
    }


async def leader_synthesize_node(state: dict, config: RunnableConfig) -> dict:
    """Leader synthesizes all Agent outputs into the final result.

    Reads agent_outputs and original task, calls LLM for comprehensive analysis,
    and generates the final final_result.

    Args:
        state: LangGraph state dictionary.
        config: Runtime configuration.

    Returns:
        State update dict containing final_result and messages.
    """
    configurable = config.get("configurable", {})
    llm_model = configurable.get("llm_model", "claude-opus-4-8")

    task = state.get("current_task", "")
    leader_plan = state.get("leader_plan", "")
    agent_outputs = state.get("agent_outputs", {})

    # Build output summary for each Agent
    outputs_text = []
    for agent_name, output in agent_outputs.items():
        outputs_text.append(f"### {agent_name}'s output:\n{output}")
    all_outputs = "\n\n".join(outputs_text) if outputs_text else "(No Agent output)"

    system_prompt = (
        "You are a team's Leader, responsible for synthesizing the team members' work products into the final result.\n"
        "Please generate a complete, coherent final result based on the original task and each member's output.\n"
        "Ensure the result is accurate and comprehensive, and blends each member's contribution.\n"
        "Output the final result directly, without extra explanation."
    )

    user_content = (
        f"## Original Task\n{task}\n\n## Execution Plan\n{leader_plan}\n\n## Member Outputs\n{all_outputs}"
    )

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
