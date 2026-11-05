"""Unit tests for the drill-down enumeration and unattributed sampling — the data-fetching layer of the `/usage` page
(2)(3) (attribution v1 §5.2).

This file watches **the three places where the presentation layer most easily twists numbers**, each corresponding to a
writing style that 'looks entirely
reasonable and quietly gives wrong answers when run':

1. **The candidate list must not carry usage.** Once a sortable list carries tokens, it will be read as a leaderboard,
   and there is no denominator in a leaderboard (§2.5). This cannot be blocked at the type level (dicts are returned),
   so it relies on tests.
2. **The rows produced by a join are not the dispatch count.** One agent may correspond to multiple workflow_agents rows
/ multiple
   task edges; counting by join rows would inflate the denominator and systematically depress the coverage.
3. **The denominator of a list row must equal the denominator of the card it opens into.** Two SQL statements written
separately would, after some change,
   quietly diverge, and nothing would error.
"""

from __future__ import annotations

import pytest

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, KnowledgeLinkModel, WorkflowAgentModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    TOKEN_LAYERS,
    AttributionScope,
    DispatchPopulation,
    TokenMetric,
)

DB = "sqlite+aiosqlite://"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    *,
    agent_id: str,
    role: str = "worker",
    name: str = "w",
    project_id: str | None = "p1",
    session_id: str | None = "s1",
    transcript_path: str | None = None,
    measured: bool = False,
) -> None:
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id="t1",
                name=name,
                role=role,
                created_at=utc_now(),
                session_id=session_id,
                project_id=project_id,
                transcript_path=transcript_path,
                input_tokens=1 if measured else None,
                output_tokens=2 if measured else None,
                cache_creation_tokens=3 if measured else None,
                cache_read_tokens=4 if measured else None,
                tokens_measured_at=utc_now() if measured else None,
            )
        )


async def _wf_agent(*, row_id: str, agent_id: str, run_id: str, cc_agent_id: str) -> None:
    async with get_session(DB) as session:
        session.add(
            WorkflowAgentModel(
                id=row_id,
                run_id=run_id,
                wf_id=run_id,
                cc_agent_id=cc_agent_id,
                os_agent_id=agent_id,
                label="x",
                state="done",
                tokens=0,
                tool_calls=0,
                created_at=utc_now(),
            )
        )


async def _worked_on(agent_id: str, task_id: str) -> None:
    async with get_session(DB) as session:
        session.add(
            KnowledgeLinkModel(
                from_kind=AGENT_TASK_LINK_FROM_KIND,
                from_id=agent_id,
                to_kind=AGENT_TASK_LINK_TO_KIND,
                to_id=task_id,
                link_type=AGENT_TASK_LINK_TYPE,
                context="",
                link_source="test",
                project_id="",
                created_at=utc_now(),
            )
        )


class TestNoUsageInCandidateList:
    """Any token layer appearing in the candidate list is a red line — that would be a leaderboard without a
    denominator."""

    @pytest.mark.asyncio
    async def test_candidate_rows_carry_counts_only(self, repo):
        await _agent(agent_id="a1", measured=True)
        rows = await repo.list_attribution_scopes(scope=AttributionScope.PROJECT)

        assert rows, "At least one project must be enumerated"
        for row in rows:
            for layer in TOKEN_LAYERS:
                assert layer not in row, (
                    f"The candidate list carried {layer} — usage must be queried separately per scope"
                )
            # Only these five keys are allowed to appear; for every extra one, first think clearly whether it is
            # impersonating a denominator
            assert set(row) == {
                "scope",
                "scope_id",
                "label",
                "dispatches_total",
                "dispatches_attributed",
            }


class TestJoinDoesNotInflateDenominator:
    """A multi-row join must not count one agent as several dispatches."""

    @pytest.mark.asyncio
    async def test_agent_in_two_workflow_rows_counted_once_per_run(self, repo):
        # The same agent left two workflow_agents rows in the same run (replay/reconnection both do this)
        await _agent(agent_id="a1", measured=True)
        await _wf_agent(row_id="r1", agent_id="a1", run_id="wf_1", cc_agent_id="cc1")
        await _wf_agent(row_id="r2", agent_id="a1", run_id="wf_1", cc_agent_id="cc1-retry")

        rows = await repo.list_attribution_scopes(scope=AttributionScope.WORKFLOW_RUN)
        assert len(rows) == 1
        assert rows[0]["dispatches_total"] == 1, "Counting by join rows would count it as 2"
        assert rows[0]["dispatches_attributed"] == 1

    @pytest.mark.asyncio
    async def test_agent_on_two_tasks_counted_once_per_task(self, repo):
        await _agent(agent_id="a1", measured=True)
        # The same edge leaving two accounts is blocked at the DB layer by knowledge_links' unique constraint,
        # so what is tested here is the other half: one agent spanning two tasks.
        await _worked_on("a1", "task-A")
        await _worked_on("a1", "task-B")

        rows = {r["scope_id"]: r for r in await repo.list_attribution_scopes(scope=AttributionScope.TASK)}
        assert set(rows) == {"task-A", "task-B"}
        assert rows["task-A"]["dispatches_total"] == 1
        # Spanning two tasks and leaving accounts → the four layers cannot be split; truthfully counted as unattributed
        # (§2.4; averaging is forbidden)
        assert rows["task-A"]["dispatches_attributed"] == 0
        assert rows["task-B"]["dispatches_attributed"] == 0


class TestListMatchesCard:
    """The denominator of a list row == the denominator of the attribution card it opens into. If they diverge,
    nothing errors; they just quietly disagree."""

    @pytest.mark.asyncio
    async def test_session_row_denominator_equals_attribution_denominator(self, repo):
        await _agent(agent_id="a1", session_id="s1", measured=True)
        await _agent(agent_id="a2", session_id="s1")
        await _agent(agent_id="a3", session_id="s2", measured=True)

        rows = {r["scope_id"]: r for r in await repo.list_attribution_scopes(scope=AttributionScope.SESSION)}
        for scope_id, row in rows.items():
            card = await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM,
                scope=AttributionScope.SESSION,
                scope_id=scope_id,
            )
            assert row["dispatches_total"] == card.dispatches_total
            assert row["dispatches_attributed"] == card.dispatches_attributed

    @pytest.mark.asyncio
    async def test_parent_filter_reuses_the_same_scope_filter(self, repo):
        await _agent(agent_id="a1", project_id="p1", session_id="s1")
        await _agent(agent_id="a2", project_id="p2", session_id="s2")

        rows = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION,
            parent_scope=AttributionScope.PROJECT,
            parent_id="p1",
        )
        assert [r["scope_id"] for r in rows] == ["s1"]


class TestScopeGuards:
    @pytest.mark.asyncio
    async def test_task_scope_rejects_parent_id(self, repo):
        # task is a side branch, not a fifth level: forcibly applying the parent filter would give a subset that looks
        # reasonable but cannot be explained
        with pytest.raises(ValueError, match="side branch"):
            await repo.list_attribution_scopes(
                scope=AttributionScope.TASK,
                parent_scope=AttributionScope.PROJECT,
                parent_id="p1",
            )

    @pytest.mark.asyncio
    async def test_null_scope_keys_are_dropped_not_grouped(self, repo):
        # Rows with a NULL session_id should not aggregate into a candidate called 'null' — that is a session that does
        # not exist
        await _agent(agent_id="a1", session_id=None)
        await _agent(agent_id="a2", session_id="s1")
        rows = await repo.list_attribution_scopes(scope=AttributionScope.SESSION)
        assert [r["scope_id"] for r in rows] == ["s1"]

    @pytest.mark.asyncio
    async def test_leader_population_is_separate_from_subagents(self, repo):
        await _agent(agent_id="a1", role="worker", session_id="s1")
        await _agent(agent_id="lead", role="leader", session_id="s9")

        sub = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION, population=DispatchPopulation.SUBAGENT
        )
        leader = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION, population=DispatchPopulation.LEADER_SESSION
        )
        assert [r["scope_id"] for r in sub] == ["s1"]
        assert [r["scope_id"] for r in leader] == ["s9"]


class TestUnattributedSamples:
    """A sample is a sample, not a denominator — the response must state clearly how many rows it scanned."""

    @pytest.mark.asyncio
    async def test_samples_are_bounded_and_declare_scan_size(self, repo):
        for i in range(12):
            await _agent(agent_id=f"a{i}", transcript_path=None)

        data = await repo.sample_unattributed(scan_limit=5, per_reason=2)
        assert data["scanned"] == 5, "The scan volume must be capped by scan_limit"
        assert data["scan_limit"] == 5
        assert len(data["samples"]["no_transcript_path"]) == 2

    @pytest.mark.asyncio
    async def test_measured_rows_never_appear_as_samples(self, repo):
        await _agent(agent_id="ok", transcript_path="/x.jsonl", measured=True)
        await _agent(agent_id="bad", transcript_path=None)

        data = await repo.sample_unattributed()
        ids = {row["agent_id"] for rows in data["samples"].values() for row in rows}
        assert ids == {"bad"}

    @pytest.mark.asyncio
    async def test_transcript_gone_is_told_apart_from_never_recorded(self, repo):
        # 'Unrecoverable' and 'not yet recovered' must be labeled separately — merging them would make this table lose
        # all its value (§3.4)
        await _agent(agent_id="never", transcript_path=None)
        await _agent(agent_id="gone", transcript_path="/nowhere/does-not-exist.jsonl")

        data = await repo.sample_unattributed()
        assert [r["agent_id"] for r in data["samples"]["no_transcript_path"]] == ["never"]
        assert [r["agent_id"] for r in data["samples"]["transcript_gone"]] == ["gone"]
