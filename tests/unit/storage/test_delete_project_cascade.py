"""Batch 3 (6): delete_project's cascade missed memories with scope='team' (the direct cause of 125 orphaned memories).

Team knowledge (failure_alchemy / lesson_learned / loop_review) is stored with scope='team' +
scope_id=<team_id>. When a project is deleted the teams rows are deleted, but nobody looked after these memories, so
from then on
their scope_id points to a team that does not exist — they can neither be queried nor deleted.

This also locks down two more sibling hazards of the 'subquery invalidated after the parent row is deleted' kind: agents
and events are both cleaned
by a team_ids subquery, and team_ids must be materialized **before** the teams rows are deleted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aiteam.storage.models import AgentModel, EventModel, MemoryModel


async def _count(repo, model, **where) -> int:
    from aiteam.storage.connection import get_session

    async with get_session(repo._db_url) as session:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_team_scoped_memories_are_cascaded(db_repository):
    project = await db_repository.create_project(name="doomed", root_path="/tmp/doomed")
    keep = await db_repository.create_project(name="keeper", root_path="/tmp/keeper")

    team = await db_repository.create_team(name="doomed-team", mode="coordinate", project_id=project.id)
    other_team = await db_repository.create_team(name="keeper-team", mode="coordinate", project_id=keep.id)

    await db_repository.create_memory(scope="team", scope_id=team.id, content="Team knowledge")
    await db_repository.create_memory(scope="team", scope_id=other_team.id, content="Another project's")
    await db_repository.create_memory(scope="project", scope_id=project.id, content="Project memory")
    await db_repository.create_memory(scope="global", scope_id="global", content="Global")

    assert await db_repository.delete_project(project.id) is True

    assert await _count(db_repository, MemoryModel, scope="team", scope_id=team.id) == 0
    # Clean up only this project's: another project's team memories + the global memories must remain exactly intact
    assert await _count(db_repository, MemoryModel, scope="team", scope_id=other_team.id) == 1
    assert await _count(db_repository, MemoryModel, scope="global") == 1
    assert await _count(db_repository, MemoryModel, scope="project", scope_id=project.id) == 0


@pytest.mark.asyncio
async def test_team_agents_and_events_are_cascaded(db_repository):
    """The team_ids subquery must be materialized before the teams rows are deleted, otherwise every table cleaned by
    it is cleaned in vain."""
    project = await db_repository.create_project(name="doomed2", root_path="/tmp/doomed2")
    team = await db_repository.create_team(name="doomed-team-2", mode="coordinate", project_id=project.id)
    await db_repository.create_agent(team_id=team.id, name="worker", role="dev")
    await db_repository.create_event(
        event_type="team.created", source=f"team:{team.id}", data={}, entity_id=team.id
    )

    assert await _count(db_repository, AgentModel, team_id=team.id) == 1
    assert await _count(db_repository, EventModel, entity_id=team.id) == 1

    assert await db_repository.delete_project(project.id) is True

    assert await _count(db_repository, AgentModel, team_id=team.id) == 0
    assert await _count(db_repository, EventModel, entity_id=team.id) == 0
