"""The agent → session hop must survive (token attribution v1, phase 1).

During design forensics, only 2 of the 2,568 rows in ``agents.session_id`` across the whole table were non-empty, so on
the attribution chain the
'session → agent' hop effectively did not exist in the database. The root cause is **three sibling instances** of
'treating identity as state and clearing it':

1. ``_on_session_end`` wipes session_id — fixed by 91e07ad;
2. ``_on_stop`` mode 2 global fallback — fixed in the same pass;
3. ``deps._startup_reconciliation`` sets the whole database's session_id to None on every API startup —
   **the instance fixed by this file** is the last of the three, and the harshest: the first two act per session,
   this one wipes the whole database in a single pass, and the API is restarted repeatedly by live CC sessions.

Production evidence: in events, five ``agent.updated`` rows within one second at 2026-07-29 01:54:54, the busy rows'
changes were ``["status","current_task","session_id"]`` and the rest were ``["session_id"]``,
matching the function's two writing styles verbatim.

The other half is the write surface: SubagentStop opportunistically pins session_id back from the transcript path, so
that
even if one hop misses a write, an agent can self-heal after finishing a single round.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.api.deps import _startup_reconciliation
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import TokenSource

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
OTHER_SESSION = "abff40af-58a1-4a7e-84ee-a68f07f72ed3"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


def _subagent_transcript(session_id: str, cc_id: str, wf_id: str | None = None) -> str:
    mid = f"workflows/{wf_id}/" if wf_id else ""
    return (
        f"/Users/dev/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
        f"{session_id}/subagents/{mid}agent-{cc_id}.jsonl"
    )


class TestStartupReconciliationKeepsIdentity:
    @pytest.mark.asyncio
    async def test_restart_resets_status_but_not_session_id(self, repo):
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="busy", current_task="digging")

        await _startup_reconciliation(repo)

        after = await repo.get_agent(agent.id)
        assert after.status.value == "waiting"
        assert after.current_task is None
        # Identity does not disappear across process restarts
        assert after.session_id == SESSION

    @pytest.mark.asyncio
    async def test_idle_rows_keep_their_session_too(self, repo):
        """Non-busy rows used to be wiped separately as well (changes=["session_id"])."""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="idle-one", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="waiting")

        await _startup_reconciliation(repo)

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_stale_waiting_agent_still_goes_offline_with_identity_intact(self, repo):
        """State cleanup still takes effect as before — what is fixed is the identity column, not the self-healing
        ability."""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="stale", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(
            agent.id, status="waiting", last_active_at=utc_now() - timedelta(hours=3)
        )

        await _startup_reconciliation(repo)

        after = await repo.get_agent(agent.id)
        assert after.status.value == "offline"
        assert after.session_id == SESSION

    @pytest.mark.asyncio
    async def test_repeated_restarts_never_erode_identity(self, repo):
        """The API may be restarted many times a day — running it several more times should not wear the identity
        away."""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="busy")

        for _ in range(3):
            await _startup_reconciliation(repo)

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_leader_reuse_survives_a_restart(self, repo, translator):
        """Regression for the 91e07ad lesson: once the identity is wiped, resuming the same session creates another
        Leader."""
        await repo.create_project(name="AI team OS", root_path="/Users/dev/Desktop/AI team OS")
        await translator.handle_event({
            "hook_event_name": "SessionStart",
            "session_id": SESSION,
            "cwd": "/Users/dev/Desktop/AI team OS",
        })

        await _startup_reconciliation(repo)
        await translator.handle_event({
            "hook_event_name": "SessionStart",
            "session_id": SESSION,
            "cwd": "/Users/dev/Desktop/AI team OS",
        })

        leaders = [
            a for t in await repo.list_teams()
            for a in await repo.list_agents(t.id)
            if a.role == "leader"
        ]
        assert len(leaders) == 1
        assert leaders[0].session_id == SESSION


class TestSubagentStopWritesSessionId:
    async def _registered_agent(self, repo, cc_id: str):
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", cc_tool_use_id=cc_id,
        )
        await repo.update_agent(agent.id, status="busy")
        return agent

    @pytest.mark.asyncio
    async def test_stop_derives_session_from_transcript_path(self, repo, translator):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            # When the payload's session_id is missing, the path can still be determined
            "session_id": "",
            "agent_transcript_path": _subagent_transcript(SESSION, "a1b2c3"),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_workflow_subagent_path_resolves_the_same_session(self, repo, translator):
        agent = await self._registered_agent(repo, "af2d08")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "af2d08",
            "agent_type": "worker",
            "session_id": "",
            "agent_transcript_path": _subagent_transcript(
                SESSION, "af2d08", wf_id="wf_8cd4fced-95a"
            ),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_file_truth_beats_a_disagreeing_payload(self, repo, translator):
        """The path is the file truth; the payload is only a retelling — when they conflict, the path wins."""
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": OTHER_SESSION,
            "agent_transcript_path": _subagent_transcript(SESSION, "a1b2c3"),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_payload_session_is_the_fallback_when_path_is_unparseable(
        self, repo, translator
    ):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": SESSION,
            "agent_transcript_path": "/tmp/not-a-transcript.jsonl",
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_no_evidence_means_no_write_never_a_guess(self, repo, translator):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": "",
            "agent_transcript_path": "",
        })

        assert (await repo.get_agent(agent.id)).session_id is None


class TestTokensSourceIsStampedOnTheSamePass:
    """As the D1 five columns are written, it must be made clear 'how this number came to be' (phase 0 §2.6).

    A NULL ``tokens_source`` means **not yet collected**. If a number is collected without stamping its source, the read
    side
    cannot tell 'pinned true by the transcript' from 'derived by alias fallback', and the two differ by orders of
    magnitude in credibility.
    """

    @pytest.mark.asyncio
    async def test_transcript_measured_row_is_stamped_transcript(
        self, repo, translator, tmp_path
    ):
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", cc_tool_use_id="a1b2c3",
        )
        tpath = tmp_path / "agent-a1b2c3.jsonl"
        tpath.write_text(json.dumps({
            "type": "assistant",
            "requestId": "req_1",
            "message": {
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "cache_creation_input_tokens": 33,
                    "cache_read_input_tokens": 44,
                },
            },
        }) + "\n")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": SESSION,
            "agent_transcript_path": str(tpath),
        })

        after = await repo.get_agent(agent.id)
        assert after.input_tokens == 11
        assert after.cache_read_tokens == 44
        assert after.tokens_source == TokenSource.TRANSCRIPT

    @pytest.mark.asyncio
    async def test_unmeasured_row_keeps_a_null_source(self, repo, translator):
        """If nothing can be collected, leave it empty — NULL means 'not yet collected', not 'a 0 of unknown origin'."""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", cc_tool_use_id="a1b2c3",
        )

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": SESSION,
            "agent_transcript_path": "/nowhere/agent-a1b2c3.jsonl",
        })

        after = await repo.get_agent(agent.id)
        assert after.tokens_source is None
        assert after.input_tokens is None
