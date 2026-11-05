"""AI Team OS — standalone company mode (no Claude Code required).

The Muse company org, runnable end-to-end on the OS's own stack:

- **Chairman** — the human owner. Sets the mission, approves external or
  irreversible actions. Not an agent.
- **CEO** — the Leader in the coordinate graph (``leader_plan`` /
  ``leader_synthesize`` nodes). Breaks the mission into a plan, delegates,
  synthesizes the final result.
- **COO** — operations agent on the team: planning, coordination,
  follow-ups, delivery tracking.
- **CTO** — technology-lead agent: architecture and technical decisions.
- **Engineering / QA agents** — builders and verification.

Execution goes through :class:`~aiteam.orchestrator.team_manager.TeamManager`
with a direct-API (or offline fake) chat model from
:mod:`aiteam.orchestrator.llm_factory`. Nothing here reads ``~/.claude``,
installs hooks, or shells out to a ``claude`` binary — the API server,
dashboard, task wall and this runner are all Claude-Code-free. Features that
observe Claude Code itself (session fleet, CC workflow ingest, hooks) remain
Claude Code features and are simply unused in this mode.
"""

from __future__ import annotations

from typing import Any

from aiteam.orchestrator.llm_factory import build_llm
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskResult, Team

COMPANY_TEAM_NAME = "muse-company"

# (name, role, system prompt) — the CEO is the graph's Leader, so the team
# itself carries the COO, CTO and the execution roles the CEO delegates to.
COMPANY_ROLES: list[tuple[str, str, str]] = [
    (
        "coo",
        "Chief Operating Officer (COO)",
        "You are the COO of an AI company run for its Chairman. You turn the "
        "CEO's plan into operations: sequence the work, track deliverables "
        "and deadlines, coordinate hand-offs between roles, surface blockers "
        "early with an impact note, and report status in concrete numbers. "
        "You do not change strategy or make technical decisions yourself.",
    ),
    (
        "cto",
        "Chief Technology Officer (CTO)",
        "You are the CTO of an AI company. You own architecture and technical "
        "decisions: choose the approach, define interfaces and acceptance "
        "criteria, flag technical risks, and review the engineers' output "
        "for quality before it is accepted.",
    ),
    (
        "backend-engineer",
        "Backend Engineer",
        "You are a backend engineer. You implement the server-side part of "
        "the assigned subtask: APIs, data models and business logic, with "
        "attention to correctness and clear interfaces.",
    ),
    (
        "frontend-engineer",
        "Frontend Engineer",
        "You are a frontend engineer. You implement the user-facing part of "
        "the assigned subtask: screens, components and interactions, with "
        "attention to usability and consistency.",
    ),
    (
        "qa-engineer",
        "QA Engineer",
        "You are a QA engineer. You verify the assigned subtask against its "
        "acceptance criteria: design checks, run through failure cases, and "
        "report precisely what passes, what fails, and what is untested. "
        "Nothing is 'done' until you say what you verified.",
    ),
]


async def ensure_company_team(manager: TeamManager) -> Team:
    """Create the Muse company team if needed and add any missing roles.

    Idempotent: an existing team is reused and only missing role agents are
    added, so repeated standalone runs share one team and one task history.
    """
    from aiteam.api.exceptions import NotFoundError

    try:
        team = await manager.get_team(COMPANY_TEAM_NAME)
    except NotFoundError:
        team = await manager.create_team(COMPANY_TEAM_NAME, mode="coordinate")

    existing = {agent.name for agent in await manager.list_agents(COMPANY_TEAM_NAME)}
    for name, role, system_prompt in COMPANY_ROLES:
        if name not in existing:
            await manager.add_agent(
                COMPANY_TEAM_NAME,
                name=name,
                role=role,
                system_prompt=system_prompt,
            )
    return team


async def run_company_task(
    task_description: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    db_url: str | None = None,
    responses: list[str] | None = None,
) -> TaskResult:
    """Run one task through the standalone company, end to end.

    Args:
        task_description: What the Chairman wants done.
        provider: LLM provider (``anthropic`` | ``openai`` | ``fake``);
            defaults to ``AITEAM_LLM_PROVIDER`` env, then ``anthropic``.
        model: Provider model name; provider default when omitted.
        db_url: SQLAlchemy URL for the OS database. Defaults to the OS's
            standard SQLite location (same DB the API/dashboard use).
        responses: Canned replies for the ``fake`` provider (tests/demos).

    Returns:
        The :class:`TaskResult` with the CEO's synthesized result and each
        role agent's output.
    """
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    manager = TeamManager(repository=repo, memory=None)
    await ensure_company_team(manager)
    llm: Any = build_llm(provider=provider, model=model, responses=responses)
    return await manager.run_task(COMPANY_TEAM_NAME, task_description, llm=llm)
