"""``agent --worked_on--> task`` Parasitic writing of the edge — token usage attribution v1 §2.4.

**Why not add a task_id column to agents**: the ``SubagentStart`` payload only contains
agent_id / agent_type / session_id / cc_team_name, with no prompt and no task identifier of any kind.
At the moment an agent is born, the OS has no way to know which task it is working for. That column would stay NULL for
a long time,
and in an attribution context a NULL is very easily read as "this work cost no tokens" — adding a column that cannot be
filled is worse than not adding one
 (§2.3).

**Why not add a new "bind agent to task" tool**: within 22 days, ``task_memo_add`` was called
711 times and ``task_update`` 179 times, while "any design that requires a human to proactively make one extra tool call
rarely survives" has been
verified repeatedly. So the approach here is: write the edge down inside bookkeeping behavior that **already necessarily
happens** — these three
tools' call parameters naturally carry both ``task_id`` and ``author`` (§2.4).

The edge's semantic boundary (written here because it is very easily read as something else): this edge says "this agent
left records on this task", **not** "all of this agent's tokens belong to this task". When one agent
spans multiple tasks, the attribution deriver honestly counts it as task-level unattributed and does not
average-allocate it (see
``repository._single_worked_on_agents_subquery``).
"""

from __future__ import annotations

import logging

from fastapi import Request

from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_SOURCE,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    KnowledgeLink,
)

logger = logging.getLogger(__name__)

# The MCP side puts the current CC session id in this header (see mcp/_base._api_call). Without it, the domain cannot be
# resolved
# and no edge is written — see the resolve_author_agent_id documentation.
SESSION_HEADER = "X-CC-Session-Id"


def session_id_from_request(request: Request | None) -> str:
    """Get the current CC session id from the request headers; return an empty string when unavailable (the caller
    then gives up writing the edge)."""
    if request is None:
        return ""
    return (request.headers.get(SESSION_HEADER) or "").strip()


async def resolve_author_agent_id(
    repo: StorageRepository, author: str, session_id: str
) -> str | None:
    """Resolve the ``author`` name in a bookkeeping action into an agent row id; return None when it cannot be resolved.

    Two tiers, both **restricted to the current session's domain**, taking the latest by ``created_at`` within the
    domain:

    Tier 1: a hit on ``agents.session_id`` — the authoritative source of identity. After stage 1 backfilled values from
    transcript paths,
        78.5% of sub-agent rows have a value.
    Tier 2: in a team under that session's name (``teams.config.owner_session_id`` / ``session-<sid8>``),
        the same-named row. This tier is not redundant: measured in practice, 0 of 117 Leader rows have a non-NULL
        ``session_id``
        (they were historically wiped by the startup sweep; stage 1 fixed the root cause, but the old rows cannot be
        recovered), and the Leader happens to be
        the most frequent author of bookkeeping actions; without this tier it could not build a single edge.

    **If it cannot be resolved, do not write it**, and never fall back to "searching the whole table by name for the
    latest". This is not conservatism; it is because doing so inevitably
    Mis-binding: ``name`` is not unique in the agents table — measured in practice, the single name ``Leader`` spans 117
    rows across 79 teams,
    over a three-week span. A mis-bound edge written from a cross-time name collision still queries fine and raises no
    error; it only quietly makes task-level attribution
    point at another agent in another session — an error nobody would ever discover. Better one fewer edge.
    """
    author = (author or "").strip()
    if not author or not session_id:
        return None

    agent = await repo.find_latest_agent_by_name(author, session_id=session_id)
    if agent is None:
        team_ids = await repo.find_teams_by_owner_session(session_id)
        if not team_ids:
            return None
        agent = await repo.find_latest_agent_by_name(author, team_ids=team_ids)
    return agent.id if agent is not None else None


async def record_worked_on(
    repo: StorageRepository,
    *,
    task_id: str,
    author: str,
    session_id: str,
    project_id: str = "",
    origin: str = "",
) -> str | None:
    """Record one ``agent --worked_on--> task`` edge; return the bound agent id, or None if nothing was bound.

    Idempotent: the ``knowledge_links`` five-tuple UNIQUE keeps only one edge for the same (agent, task); repeated
    bookkeeping does not pile up. ``origin`` only enters the context for later auditing (which bookkeeping action
    brought this edge about).

    Callers uniformly wrap this as best-effort: **attribution is observation; broken observation must not drag down the
    bookkeeping action being observed**.
    """
    if not task_id:
        return None
    agent_id = await resolve_author_agent_id(repo, author, session_id)
    if agent_id is None:
        return None
    await repo.insert_knowledge_links([
        KnowledgeLink(
            from_kind=AGENT_TASK_LINK_FROM_KIND,
            from_id=agent_id,
            to_kind=AGENT_TASK_LINK_TO_KIND,
            to_id=task_id,
            link_type=AGENT_TASK_LINK_TYPE,
            context=f"author={author} via={origin}" if origin else f"author={author}",
            link_source=AGENT_TASK_LINK_SOURCE,
            project_id=project_id or "",
        )
    ])
    return agent_id


async def try_record_worked_on(
    repo: StorageRepository,
    *,
    task_id: str,
    author: str,
    request: Request | None,
    project_id: str = "",
    origin: str = "",
) -> None:
    """The best-effort wrapper called by the route layer — any failure is only logged and never affects the
    bookkeeping action itself."""
    try:
        agent_id = await record_worked_on(
            repo,
            task_id=task_id,
            author=author,
            session_id=session_id_from_request(request),
            project_id=project_id,
            origin=origin,
        )
        if agent_id is None:
            logger.debug(
                "worked_on edge skipped: author=%r unresolvable in session domain (%s)",
                author,
                origin,
            )
    except Exception:  # noqa: BLE001 — an observation failure must not drag down the action being observed
        logger.warning("worked_on edge write failed (%s)", origin, exc_info=True)
