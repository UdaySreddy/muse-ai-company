"""Unit tests for the parasitic writing of agent→task edges — attribution v1 §2.4.

This file's focus is entirely on the rule **if it cannot be resolved, do not write**. That `agents.name` is not unique
is a measured fact, not
a theoretical risk: in the production database, one name, ``Leader``, corresponds to 117 rows spanning 79 teams over
three weeks.
An edge written with a cross-time same-name mis-binding can still be queried and still raises no error; it would only
make task-level attribution quietly point to another
session's another agent — an error nobody would ever discover. So one edge fewer is preferable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiteam.api.task_edge import (
    SESSION_HEADER,
    record_worked_on,
    resolve_author_agent_id,
    session_id_from_request,
)
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, TeamModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TYPE,
    AttributionScope,
    TokenMetric,
)

DB = "sqlite+aiosqlite://"
SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
OTHER_SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    *,
    agent_id: str,
    name: str,
    session_id: str | None = None,
    team_id: str = "t1",
    created_at=None,
    role: str = "worker",
) -> None:
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id=team_id,
                name=name,
                role=role,
                session_id=session_id,
                created_at=created_at or utc_now(),
            )
        )


async def _team(*, team_id: str, name: str, owner_session_id: str | None = None) -> None:
    config = {"kind": "session"}
    if owner_session_id:
        config["owner_session_id"] = owner_session_id
    async with get_session(DB) as session:
        session.add(TeamModel(id=team_id, name=name, config=config, created_at=utc_now()))


async def _edges(repo: StorageRepository, task_id: str) -> list:
    links = await repo.find_knowledge_links("task", task_id, direction="in")
    return [
        lk
        for lk in links
        if lk.from_kind == AGENT_TASK_LINK_FROM_KIND and lk.link_type == AGENT_TASK_LINK_TYPE
    ]


class TestAuthorResolutionIsDomainBound:
    @pytest.mark.asyncio
    async def test_resolves_within_current_session(self, repo):
        await _agent(agent_id="mine", name="backend-architect", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "backend-architect", SESSION) == "mine"

    @pytest.mark.asyncio
    async def test_same_name_in_another_session_is_never_bound(self, repo):
        """Leader review point: guard against cross-time same-name mis-binding.

        Another session had a same-name agent three weeks ago, and this session does not — the correct behavior is **not
        to write**,
        not 'use a same-name one if one is found'.
        """
        await _agent(
            agent_id="stranger",
            name="backend-architect",
            session_id=OTHER_SESSION,
            team_id="t-other",
            created_at=utc_now() - timedelta(days=21),
        )
        assert await resolve_author_agent_id(repo, "backend-architect", SESSION) is None

    @pytest.mark.asyncio
    async def test_newer_in_domain_row_wins_over_older_in_domain_row(self, repo):
        """Within the same session, take the same-name row with the newest ``created_at`` (an agent may be
        re-dispatched many times)."""
        await _agent(
            agent_id="old",
            name="worker",
            session_id=SESSION,
            created_at=utc_now() - timedelta(hours=5),
        )
        await _agent(agent_id="new", name="worker", session_id=SESSION, created_at=utc_now())
        assert await resolve_author_agent_id(repo, "worker", SESSION) == "new"

    @pytest.mark.asyncio
    async def test_older_in_domain_beats_newer_out_of_domain(self, repo):
        """Domain takes priority over freshness — a same-name row just created in another session does not count,
        however new."""
        await _agent(
            agent_id="mine-old",
            name="worker",
            session_id=SESSION,
            created_at=utc_now() - timedelta(days=1),
        )
        await _agent(
            agent_id="theirs-new",
            name="worker",
            session_id=OTHER_SESSION,
            team_id="t-other",
            created_at=utc_now(),
        )
        assert await resolve_author_agent_id(repo, "worker", SESSION) == "mine-old"

    @pytest.mark.asyncio
    async def test_team_fallback_covers_leader_rows_with_null_session(self, repo):
        """The Leader row's ``session_id`` measures 0/117, all NULL (wiped by a historical startup sweep).

        And the Leader happens to be the most frequent author of accounting actions — without this tier it could not
        build a single edge.
        """
        await _team(team_id="t-mine", name="session-80d0cc5e", owner_session_id=SESSION)
        await _agent(
            agent_id="leader-row", name="Leader", session_id=None, team_id="t-mine", role="leader"
        )
        assert await resolve_author_agent_id(repo, "leader", SESSION) == "leader-row"

    @pytest.mark.asyncio
    async def test_team_fallback_does_not_reach_other_sessions_teams(self, repo):
        await _team(team_id="t-theirs", name="session-11111111", owner_session_id=OTHER_SESSION)
        await _agent(
            agent_id="their-leader", name="Leader", team_id="t-theirs", role="leader"
        )
        assert await resolve_author_agent_id(repo, "leader", SESSION) is None

    @pytest.mark.asyncio
    async def test_author_match_is_case_insensitive(self, repo):
        """A memo's author defaults to ``"leader"``, while the Leader row's name is ``"Leader"``."""
        await _agent(agent_id="l", name="Leader", session_id=SESSION, role="leader")
        assert await resolve_author_agent_id(repo, "LEADER", SESSION) == "l"
        assert await resolve_author_agent_id(repo, "  leader  ", SESSION) == "l"

    @pytest.mark.asyncio
    async def test_no_session_means_no_binding(self, repo):
        """Without a session header it is not written — otherwise the whole table would have to be searched by name,
        which inevitably mis-binds."""
        await _agent(agent_id="mine", name="worker", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "worker", "") is None

    @pytest.mark.asyncio
    async def test_unknown_author_is_not_invented(self, repo):
        await _agent(agent_id="mine", name="backend-architect", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "d3-p2-typelayer", SESSION) is None


class TestEdgeWriting:
    @pytest.mark.asyncio
    async def test_writes_one_edge_and_is_idempotent(self, repo):
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        first = await record_worked_on(
            repo, task_id="T1", author="w", session_id=SESSION, origin="task_memo_add"
        )
        second = await record_worked_on(
            repo, task_id="T1", author="w", session_id=SESSION, origin="task_memo_add"
        )
        assert first == second == "mine"
        edges = await _edges(repo, "T1")
        assert len(edges) == 1
        assert edges[0].from_id == "mine"

    @pytest.mark.asyncio
    async def test_unresolvable_author_writes_nothing(self, repo):
        assert (
            await record_worked_on(repo, task_id="T1", author="ghost", session_id=SESSION) is None
        )
        assert await _edges(repo, "T1") == []

    @pytest.mark.asyncio
    async def test_edge_feeds_task_scope_attribution(self, repo):
        """Once an edge is written → the attribution deriver can immediately aggregate by task. Both sides share the
        same link vocabulary."""
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T1", author="w", session_id=SESSION)
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="T1"
        )
        assert att.dispatches_total == 1

    @pytest.mark.asyncio
    async def test_second_task_makes_the_agent_unsplittable(self, repo):
        """An agent that left accounts on two tasks → both sides truthfully count as unattributed; no averaging or
        apportioning."""
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T1", author="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T2", author="w", session_id=SESSION)
        for task_id in ("T1", "T2"):
            att = await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id=task_id
            )
            assert att.dispatches_attributed == 0
            assert att.dispatches_total == 1


class TestSessionHeader:
    def test_reads_the_agreed_header(self):
        class _Req:
            headers = {SESSION_HEADER: "  abc  "}

        assert session_id_from_request(_Req()) == "abc"
        assert session_id_from_request(None) == ""

    def test_mcp_base_sends_it(self):
        """The MCP side must carry the session id, otherwise the server side can never resolve the domain and never
        writes an edge."""
        import inspect

        from aiteam.mcp import _base

        source = inspect.getsource(_base._api_call)
        assert SESSION_HEADER in source
        assert "_cc_session_id()" in source
