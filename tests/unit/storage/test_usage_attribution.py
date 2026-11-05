"""Coverage aggregation unit tests — the denominator definition and unattributed classification (attribution v1 §4.1 /
§3.4 / §2.4).

**The reason this file exists is the denominator.** §4.1, verbatim: 'The definition of the denominator must be written
dead in code and have unit tests,
because the denominator is the only place that can be quietly faked'. What R2 calls faking is not malice; it is a change
that looks entirely reasonable
— 'rows with no path cannot be measured; take them out of the denominator' — and the coverage jumps from 78% to 100%;
the number looks better,
the code reads smoothly, and nothing errors. Every assertion below corresponds to one such change.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.services.usage_coverage import classify_unattributed
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, KnowledgeLinkModel, WorkflowAgentModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    AttributionMethod,
    AttributionScope,
    DispatchPopulation,
    TokenMetric,
    TokenSource,
    UnattributedReason,
)

DB = "sqlite+aiosqlite://"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    repo: StorageRepository,
    *,
    agent_id: str,
    role: str = "worker",
    name: str = "w",
    team_id: str = "t1",
    created_at=None,
    session_id: str | None = None,
    project_id: str | None = None,
    transcript_path: str | None = None,
    measured: bool = False,
    layers: tuple[int, int, int, int] = (1, 2, 3, 4),
    tokens_source: str | None = None,
) -> None:
    """Insert an AgentModel directly — create_agent does not accept created_at / the five token columns, and what this
    file tests
    is exactly how those two things participate in the denominator and the window."""
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id=team_id,
                name=name,
                role=role,
                created_at=created_at or utc_now(),
                session_id=session_id,
                project_id=project_id,
                transcript_path=transcript_path,
                input_tokens=layers[0] if measured else None,
                output_tokens=layers[1] if measured else None,
                cache_creation_tokens=layers[2] if measured else None,
                cache_read_tokens=layers[3] if measured else None,
                tokens_measured_at=utc_now() if measured else None,
                tokens_source=tokens_source,
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


class TestDenominator:
    """The denominator = the number of rows with ``role != 'leader'``, **including** those rows without a transcript."""

    @pytest.mark.asyncio
    async def test_rows_without_transcript_stay_in_denominator(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x.jsonl", measured=True)
        await _agent(repo, agent_id="a2", transcript_path=None)
        await _agent(repo, agent_id="a3", transcript_path=None)

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        # 3 rather than 1: they must not be excluded on the grounds of 'no path, so they do not count' — that is exactly
        # passing a part off as the whole picture
        assert att.dispatches_total == 3
        assert att.dispatches_attributed == 1
        assert att.unattributed_reasons == {UnattributedReason.NO_TRANSCRIPT_PATH.value: 2}

    @pytest.mark.asyncio
    async def test_leader_rows_are_a_separate_population(self, repo):
        await _agent(repo, agent_id="a1", role="worker")
        await _agent(repo, agent_id="L1", role="leader", name="Leader")
        await _agent(repo, agent_id="L2", role="leader", name="Leader")

        sub = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        leader = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, population=DispatchPopulation.LEADER_SESSION
        )
        assert sub.dispatches_total == 1
        assert leader.dispatches_total == 2

    @pytest.mark.asyncio
    async def test_workflow_subagents_are_not_excluded(self, repo):
        """A measured 96% of dispatches are ``workflow-subagent``; moving them out of the denominator would immediately
        inflate the coverage.

        ``api/agent_reuse.py`` does have an ``_EXCLUDED_ROLES`` that excludes it together with the leader,
        but that is the governance metric of 'who can be reused'. In the attribution metric, a workflow dispatch is a
        dispatch.
        """
        await _agent(repo, agent_id="a1", role="workflow-subagent")
        await _agent(repo, agent_id="a2", role="workflow-subagent")
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.dispatches_total == 2

    @pytest.mark.asyncio
    async def test_numerator_plus_unattributed_equals_denominator(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x.jsonl", measured=True)
        await _agent(repo, agent_id="a2", transcript_path=None)
        await _agent(repo, agent_id="a3", transcript_path="/gone.jsonl")
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.dispatches_attributed + sum(att.unattributed_reasons.values()) == (
            att.dispatches_total
        )


class TestWindowIsOnCreatedAt:
    """The window falls on ``created_at``, **not** ``tokens_measured_at``.

    Windowing by measurement time would make unmeasured rows disappear together with the denominator, so the coverage
    would constantly equal 100% — and it would look
    completely reasonable ('we only count what was measured in this period'). This is the most insidious kind of
    denominator fraud.
    """

    @pytest.mark.asyncio
    async def test_old_unmeasured_row_leaves_the_window_with_its_denominator(self, repo):
        now = utc_now()
        await _agent(repo, agent_id="old", created_at=now - timedelta(days=30))
        await _agent(repo, agent_id="new", created_at=now - timedelta(days=1))

        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, since=now - timedelta(days=7), until=now
        )
        assert att.dispatches_total == 1

    @pytest.mark.asyncio
    async def test_measured_today_but_created_long_ago_is_out_of_a_recent_window(self, repo):
        """Re-collection stamps rows from three weeks ago with today's ``tokens_measured_at``.

        If windowing were by measurement time, 'the last 7 days' would suddenly gain 1,900 historical dispatch rows, all
        of them already measured
        — the recent collection rate would be washed by re-collection to nearly 100% (the 'one number masking another
        number' named by R 6.3-2).
        """
        now = utc_now()
        await _agent(
            repo,
            agent_id="backfilled",
            created_at=now - timedelta(days=21),
            transcript_path="/x.jsonl",
            measured=True,  # tokens_measured_at = now
        )
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, since=now - timedelta(days=7), until=now
        )
        assert att.dispatches_total == 0
        assert att.dispatches_attributed == 0


class TestUnattributedReasons:
    @pytest.mark.asyncio
    async def test_three_reasons_are_told_apart(self, repo, tmp_path):
        alive = tmp_path / "alive.jsonl"
        alive.write_text("{}", encoding="utf-8")

        await _agent(repo, agent_id="a1", transcript_path=None)
        await _agent(repo, agent_id="a2", transcript_path=str(tmp_path / "vanished.jsonl"))
        await _agent(repo, agent_id="a3", transcript_path=str(alive))

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.unattributed_reasons == {
            UnattributedReason.NO_TRANSCRIPT_PATH.value: 1,
            UnattributedReason.TRANSCRIPT_GONE.value: 1,
            UnattributedReason.NOT_YET_MEASURED.value: 1,
        }

    def test_classifier_separates_recoverable_from_lost(self, tmp_path):
        """``not_yet_measured`` can be made up by one re-collection run; the other two cannot. Merging the two,
        a viewer could not judge whether to act now (the re-collection window is closing, R6)."""
        assert classify_unattributed(None) == UnattributedReason.NO_TRANSCRIPT_PATH.value
        assert classify_unattributed("") == UnattributedReason.NO_TRANSCRIPT_PATH.value
        assert (
            classify_unattributed("/nope.jsonl", file_probe=lambda _p: False)
            == UnattributedReason.TRANSCRIPT_GONE.value
        )
        assert (
            classify_unattributed("/yes.jsonl", file_probe=lambda _p: True)
            == UnattributedReason.NOT_YET_MEASURED.value
        )


class TestScopes:
    @pytest.mark.asyncio
    async def test_scope_id_filters_and_empty_means_whole_ledger(self, repo):
        await _agent(repo, agent_id="a1", project_id="p1", transcript_path="/x", measured=True)
        await _agent(repo, agent_id="a2", project_id="p2", transcript_path="/x", measured=True)

        whole = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        scoped = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.PROJECT, scope_id="p1"
        )
        assert whole.dispatches_total == 2
        assert scoped.dispatches_total == 1
        assert scoped.scope_id == "p1"

    @pytest.mark.asyncio
    async def test_layers_are_summed_only_over_attributed_rows(self, repo):
        await _agent(
            repo, agent_id="a1", transcript_path="/x", measured=True, layers=(1, 2, 3, 4)
        )
        await _agent(
            repo, agent_id="a2", transcript_path="/x", measured=True, layers=(10, 20, 30, 40)
        )
        await _agent(repo, agent_id="a3", transcript_path=None)
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert (att.input_tokens, att.output_tokens) == (11, 22)
        assert (att.cache_creation_tokens, att.cache_read_tokens) == (33, 44)

    @pytest.mark.asyncio
    async def test_alias_fallback_downgrades_the_whole_answer(self, repo):
        await _agent(
            repo,
            agent_id="a1",
            transcript_path="/x",
            measured=True,
            tokens_source=TokenSource.TRANSCRIPT.value,
        )
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.method is AttributionMethod.TRANSCRIPT_PARSE

        await _agent(
            repo,
            agent_id="a2",
            transcript_path="/y",
            measured=True,
            tokens_source=TokenSource.ALIAS_FALLBACK.value,
        )
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        # One fallback row downgrades the whole: honesty of the metric takes priority over looking good
        assert att.method is AttributionMethod.ALIAS_FALLBACK

    @pytest.mark.asyncio
    async def test_ctx_last_is_refused_with_the_reason(self, repo):
        """ctx_last structurally cannot come in: the four-layer decomposition is merged at ingest time and was never
        persisted.

        Putting the knowledge on the rejection path makes it harder to bypass than putting it in a comment.
        """
        with pytest.raises(ValueError, match="four layers"):
            await repo.aggregate_token_attribution(metric=TokenMetric.CTX_LAST)


class TestTaskScope:
    """§2.4: an agent spanning multiple tasks is truthfully counted as unattributed; **averaging/apportioning is
    forbidden**."""

    @pytest.mark.asyncio
    async def test_single_edge_agent_attributes_cleanly(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True, layers=(1, 2, 3, 4))
        await _worked_on("a1", "task-A")
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="task-A"
        )
        assert (att.dispatches_attributed, att.dispatches_total) == (1, 1)
        assert att.output_tokens == 2

    @pytest.mark.asyncio
    async def test_multi_task_agent_is_unattributed_not_split(self, repo):
        await _agent(
            repo, agent_id="a1", transcript_path="/x", measured=True, layers=(10, 20, 30, 40)
        )
        await _worked_on("a1", "task-A")
        await _worked_on("a1", "task-B")

        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="task-A"
        )
        assert att.dispatches_total == 1
        assert att.dispatches_attributed == 0
        assert att.unattributed_reasons == {
            UnattributedReason.MULTI_TASK_UNSPLITTABLE.value: 1
        }
        # Key point: not a single token may be apportioned over. Averaging would create unfalsifiable numbers.
        assert (att.input_tokens, att.output_tokens) == (0, 0)
        assert (att.cache_creation_tokens, att.cache_read_tokens) == (0, 0)

    @pytest.mark.asyncio
    async def test_empty_task_id_is_refused_instead_of_mislabelling(self, repo):
        """An empty task_id once made rows with 'fundamentally no edge' be marked as 'cannot be split'.

        The two are handled in completely opposite ways: the former relies on accounting actions to accumulate edges
        (the narrowest hop in the whole chain, 0.8%),
        the latter has the edge but the numbers cannot be split. Mixed into one number, a viewer would never see the
        real bottleneck again.
        """
        await _agent(repo, agent_id="a1")
        with pytest.raises(ValueError, match="scope_id"):
            await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id=""
            )

    @pytest.mark.asyncio
    async def test_task_with_no_edges_is_zero_over_zero(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True)
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="nobody"
        )
        assert (att.dispatches_attributed, att.dispatches_total) == (0, 0)


class TestCoverageReport:
    @pytest.mark.asyncio
    async def test_matrix_has_all_four_paths_and_no_total_row(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True)
        await _agent(repo, agent_id="L1", role="leader", name="Leader")
        async with get_session(DB) as session:
            session.add(
                WorkflowAgentModel(
                    id="wa1", run_id="wf_1", wf_id="wf_1", cc_agent_id="c1", tokens=100
                )
            )
            session.add(
                WorkflowAgentModel(
                    id="wa2", run_id="wf_1", wf_id="wf_1", cc_agent_id="c2", tokens=0
                )
            )

        report = await repo.usage_coverage_report()
        paths = [r.path for r in report.rows]
        assert paths == [
            DispatchPopulation.SUBAGENT,
            DispatchPopulation.LEADER_SESSION,
            DispatchPopulation.WORKFLOW_SELF_REPORT,
            DispatchPopulation.TOOL_CALL,
        ]
        by_path = {r.path: r for r in report.rows}
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].metric == (
            TokenMetric.CTX_LAST.value
        )
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].dispatches_attributed == 1
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].unattributed_reasons == {
            UnattributedReason.SELF_REPORT_ABSENT.value: 1
        }

    @pytest.mark.asyncio
    async def test_not_collected_row_is_none_not_zero(self, repo):
        """"Not collected by design" is a formal value. A 0 would be read as 'never measured once', and a blank would
        be read as a bug."""
        report = await repo.usage_coverage_report()
        tool_row = next(r for r in report.rows if r.path is DispatchPopulation.TOOL_CALL)
        assert tool_row.dispatches_total is None
        assert tool_row.dispatches_attributed is None
        assert tool_row.metric == ""

    @pytest.mark.asyncio
    async def test_metrics_are_never_mixed_across_rows(self, repo):
        """The matrix offers no total row: the two metrics measure 5-25x apart, so summing across rows would mix
        metrics."""
        report = await repo.usage_coverage_report()
        metrics = {r.metric for r in report.rows if r.metric}
        assert metrics == {TokenMetric.USAGE_SUM.value, TokenMetric.CTX_LAST.value}
        assert not hasattr(report, "total")

    @pytest.mark.asyncio
    async def test_hops_cover_the_whole_chain_and_share_one_denominator(self, repo):
        await _agent(repo, agent_id="a1", session_id="s1", project_id="p1", transcript_path="/x")
        await _agent(repo, agent_id="a2")
        await _worked_on("a1", "task-A")

        report = await repo.usage_coverage_report()
        hops = {h.edge: h for h in report.hops}
        assert set(hops) == {
            "agent->session",
            "agent->project",
            "agent->workflow",
            "agent->transcript",
            "agent->task",
        }
        assert all(h.required == 2 for h in hops.values())
        assert hops["agent->session"].resolvable == 1
        assert hops["agent->task"].resolvable == 1
        assert hops["agent->workflow"].resolvable == 0
