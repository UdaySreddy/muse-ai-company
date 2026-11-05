"""Periodic cleanup of completed session container teams — criteria and cascades.

A CC session gets one container team and never reuses it across sessions (fleet layer §3), so a long-running CC process
Every time it changes session it leaves a completed empty shell in the database. Production database measurement,
2026-07-27: of 80 container
teams, 78 were already completed, all with zero tasks, zero meetings and zero busy members — pure accumulation.

These cases pin down the cleanup's **restraint**. Live teams, non-container teams, recent teams, teams with someone
still busy, and
any team carrying substantive records (tasks/meetings/reports/schedules/wakes/workflow runs) must not be deleted, not a
single one;
when truly deleting, the members and their activities must be taken away together, leaving no dangling rows.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from aiteam.clock import utc_now
from aiteam.storage.connection import get_session
from aiteam.storage.models import AgentActivityModel, AgentModel, TeamModel

CONTAINER = {"kind": "session", "owner_session_id": "0def8f84-3b72-4b09-ae42-b13365ffeb65"}


async def _count(repo, model, **where) -> int:
    async with get_session(repo._db_url) as session:
        stmt = select(func.count()).select_from(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return (await session.execute(stmt)).scalar_one()


async def _age(repo, team_id: str, days: float) -> None:
    """Push all three of the team's timestamps older together.

    Production measurements: the ``completed_at`` of all 13 teams awaiting cleanup was **NULL** (the team-closing path
    writes the status
    without recording a timestamp), so age determination must be able to fall back to updated_at — completed_at is
    deliberately kept
    empty here, so the case walks exactly the production path.
    """
    stamp = utc_now() - timedelta(days=days)
    async with get_session(repo._db_url) as session:
        row = (
            await session.execute(select(TeamModel).where(TeamModel.id == team_id))
        ).scalar_one()
        row.created_at = stamp
        row.updated_at = stamp


async def _husk(repo, name: str = "session-0def8f84", *, days: float = 30):
    """A typical empty shell: a completed container team + one offline member + some member activities."""
    team = await repo.create_team(name=name, mode="coordinate", config=dict(CONTAINER))
    agent = await repo.create_agent(
        team_id=team.id, name="worker", role="worker", source="hook"
    )
    await repo.update_agent(agent.id, status="offline")
    await repo.create_activity(
        agent_id=agent.id, session_id="0def8f84", tool_name="Read"
    )
    await repo.update_team(team.id, status="completed")
    await _age(repo, team.id, days)
    return team, agent


@pytest.mark.asyncio
async def test_spent_husk_is_purged_with_its_roster(db_repository):
    team, agent = await _husk(db_repository)

    purged = await db_repository.purge_stale_session_containers(
        retention_days=7, limit=20
    )

    assert [p["team_id"] for p in purged] == [team.id]
    assert purged[0]["agents_deleted"] == 1
    assert purged[0]["activities_deleted"] == 1
    assert await db_repository.get_team(team.id) is None
    assert await _count(db_repository, AgentModel, team_id=team.id) == 0
    assert await _count(db_repository, AgentActivityModel, agent_id=agent.id) == 0


@pytest.mark.asyncio
async def test_live_container_is_never_touched(db_repository):
    """A live container team — the one this session is using — must never be swept in."""
    team = await db_repository.create_team(
        name="session-80d0cc5e", mode="coordinate", config=dict(CONTAINER)
    )
    await _age(db_repository, team.id, 90)  # Old, but still active

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_recent_husk_is_kept(db_repository):
    team, _ = await _husk(db_repository, days=3)

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_busy_member_blocks_purge(db_repository):
    """A team completed yet carrying a busy member is a known contradictory state (the reaper has separate revival
    logic) —
    such a team must be left to the revival path; the cleanup must never rush ahead and delete the people together
    with the team."""
    team, agent = await _husk(db_repository)
    await db_repository.update_agent(agent.id, status="busy")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_recently_active_member_blocks_purge(db_repository):
    """A member's recent activity is harder evidence of 'still in use' than any timestamp on the team (the 2026-07-25
    wenge lesson)."""
    team, agent = await _husk(db_repository)
    await db_repository.update_agent(agent.id, last_active_at=utc_now())

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "config"),
    [
        ("workflow-wf_374d39b3", {"kind": "workflow"}),
        ("session-legacy", {}),  # A legacy team with no kind
        ("ecosystem-platform", {"kind": "session"}),  # Has a kind but is not container naming
    ],
)
async def test_non_container_teams_are_never_touched(db_repository, name, config):
    """Dual criteria: kind=session **and** the name is session-<sid8>; if either is missing, do not delete."""
    team = await db_repository.create_team(name=name, mode="coordinate", config=config)
    await db_repository.update_team(team.id, status="completed")
    await _age(db_repository, team.id, 90)

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_tasks_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    await db_repository.create_task(team_id=team.id, title="Work still remaining")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_meetings_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    await db_repository.create_meeting(team_id=team.id, topic="A meeting that was held")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_reports_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    from aiteam.types import Report

    await db_repository.create_report(
        Report(author="worker", topic="For the record", content="# Body text", team_id=team.id)
    )

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_limit_caps_one_cycle(db_repository):
    for i in range(5):
        await _husk(db_repository, name=f"session-husk{i}")

    purged = await db_repository.purge_stale_session_containers(
        retention_days=7, limit=2
    )

    assert len(purged) == 2
    assert await _count(db_repository, TeamModel) == 3


@pytest.mark.asyncio
async def test_non_positive_retention_disables_the_purge(db_repository):
    """The kill switch must really kill: 0 days does not mean 'delete everything immediately'."""
    team, _ = await _husk(db_repository)

    assert await db_repository.purge_stale_session_containers(retention_days=0) == []
    assert await db_repository.purge_stale_session_containers(retention_days=-1) == []
    assert await db_repository.get_team(team.id) is not None


class _RecordingBus:
    """A type-checking stub — a stub must never be more lenient than production (the batch 9 lesson: when an enum
    item is missing, the unit tests are green and production blows up)."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kwargs):
        from aiteam.types import EventType

        EventType(event_type)
        self.events.append((event_type, source, data))


@pytest.mark.asyncio
async def test_reaper_sweeps_and_leaves_a_trace(db_repository):
    from aiteam.api.state_reaper import StateReaper

    team, _ = await _husk(db_repository)
    bus = _RecordingBus()
    reaper = StateReaper(repo=db_repository, event_bus=bus)

    await reaper._purge_spent_session_containers(db_repository)

    assert await db_repository.get_team(team.id) is None
    ((event_type, source, data),) = bus.events
    assert event_type == "team.container_purged"
    assert source == f"team:{team.id}"
    assert data["name"] == "session-0def8f84"


@pytest.mark.asyncio
async def test_purge_rides_the_existing_reap_tick(db_repository, monkeypatch):
    """The no-timer iron law: the cleanup rides the existing reap loop and must not start any new scheduling."""
    from aiteam.api.state_reaper import StateReaper

    reaper = StateReaper(repo=db_repository, event_bus=_RecordingBus())
    called: list[object] = []

    async def spy(repo=None):
        called.append(repo)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(reaper, "_purge_spent_session_containers", spy)
    for heavy in (
        "_check_default_model_health",
        "_check_agent_liveness",
        "_backfill_agent_watermarks",
        "_check_scheduled_tasks",
        "_check_workflow_ingest",
    ):
        monkeypatch.setattr(reaper, heavy, noop)

    await reaper._reap_cycle_for_repo(db_repository)

    assert called == [db_repository]


@pytest.mark.asyncio
async def test_dry_run_reports_without_deleting(db_repository):
    team, agent = await _husk(db_repository)

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert [p["team_id"] for p in planned] == [team.id]
    assert await db_repository.get_team(team.id) is not None
    assert await _count(db_repository, AgentModel, team_id=team.id) == 1
    assert await _count(db_repository, AgentActivityModel, agent_id=agent.id) == 1


@pytest.mark.asyncio
async def test_member_carrying_token_ledger_blocks_purge(db_repository):
    """A usage account hanging on a member row = never clean it up (rule established from the 2026-08-03 record).

    This gate differs from the tasks/meetings/reports ones: those ask 'who else will come to look at this record',
    while this one asks 'would deleting it destroy data that can never be collected again'. The five token columns are
    parsed out of
    transcripts, and transcripts are cleared over time — once the row is deleted, they are gone forever.

    Record: purge went live with v1.11.0 (2026-07-27), while the five token columns did not land on agent rows until
    v1.11.1 (2026-07-29),
    and the cleanup gate was never retrofitted. A production database measurement found an empty shell closed on 07-27
    carrying
    85,418,776 tokens, entering range four hours later; in the same period 16 rows had already been deleted after the
    token columns landed.
    """
    team, agent = await _husk(db_repository, name="session-1a2b3c4d")
    await db_repository.update_agent(agent.id, cache_read_tokens=63_178_891)

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert team.id not in [p["team_id"] for p in planned]


@pytest.mark.asyncio
async def test_measured_zero_also_blocks_purge(db_repository):
    """A measured 0 is also a measurement result; it is not the same thing as 'never measured' (no-data != zero)."""
    team, agent = await _husk(db_repository, name="session-2b3c4d5e")
    await db_repository.update_agent(agent.id, tokens_measured_at=utc_now())

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert team.id not in [p["team_id"] for p in planned]


@pytest.mark.asyncio
async def test_husk_without_token_ledger_is_still_purged(db_repository):
    """Reverse: an empty shell with no account is cleaned up as usual; this gate must not abolish purge altogether."""
    team, _ = await _husk(db_repository, name="session-3c4d5e6f")

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert team.id in [p["team_id"] for p in planned]


@pytest.mark.asyncio
async def test_unharvested_transcript_blocks_purge(db_repository, tmp_path):
    """Has a transcript_path, the account not yet collected, and the file still present = must not delete (gate added
    from the 2026-08-03 forensics).

    The path is recorded only on the row: once the row is deleted, even if the transcript still lies on disk,
    re-collection can never find
    it — the window silently closes, the same failure mode as destroying recorded accounts, just one step earlier.
    Production measurement:
    9 empty-shell teams outside the gate carried 10 uncollected Leader rows, and all 10 transcripts are still on disk.
    """
    transcript = tmp_path / "87500462-f517-473b-9bc2-468a1ab7892f.jsonl"
    transcript.write_text('{"type":"assistant"}\n')
    team, agent = await _husk(db_repository, name="session-4d5e6f7a")
    await db_repository.update_agent(agent.id, transcript_path=str(transcript))

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert team.id not in [p["team_id"] for p in planned]


@pytest.mark.asyncio
async def test_dead_transcript_path_does_not_block_purge(db_repository, tmp_path):
    """Reverse: a row whose file has already been cleared by CC has nothing to collect and falls back to an ordinary
    husk, cleaned up as usual."""
    team, agent = await _husk(db_repository, name="session-5e6f7a8b")
    await db_repository.update_agent(
        agent.id, transcript_path=str(tmp_path / "gone.jsonl")
    )

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert team.id in [p["team_id"] for p in planned]
