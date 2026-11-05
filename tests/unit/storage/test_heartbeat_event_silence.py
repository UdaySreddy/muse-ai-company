"""Q2 — Pure heartbeats no longer record events, while heartbeat **detection** must remain utterly unchanged.

The Creator ruled on 2026-07-27 to stop the writes, with a hard condition: 'Stopping heartbeat writes is not stopping
heartbeat detection, is it?
As long as it does not affect other detection features, it is fine.' This is exactly what this file pins down.

Scale of the stopped writes (measured in the production database): ``agent.updated`` has 112,349 rows in total = 47.4%
of the whole events table's
237,126 rows; of those, 94,029 rows have changes of exactly ``last_active_at`` only,
and that single class alone accounts for 39.7% of the whole table (45,717 / 118,867 = 38.5% in the last 7 days).

What is stopped and what is not:
- Stopped: the event row that used to be written opportunistically after ``update_agent(last_active_at=…)``.
- Not stopped: the ``agents.last_active_at`` column is still written. The StateReaper timeout criterion,
  wake_actionable's 'just finished work' criterion — both read this column, not the event stream.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def agent(repo):
    team = await repo.create_team(name="t", mode="coordinate")
    a = await repo.create_agent(team_id=team.id, name="worker", role="worker", source="hook")
    yield await repo.get_agent(a.id)


async def _agent_events(repo, agent_id: str) -> list:
    return await repo.list_events(event_type="agent.updated", entity_id=agent_id, limit=100)


class TestWriteStop:
    @pytest.mark.asyncio
    async def test_pure_heartbeat_writes_the_column_but_no_event(self, repo, agent):
        beat = utc_now()
        await repo.update_agent(agent.id, last_active_at=beat)

        assert (await repo.get_agent(agent.id)).last_active_at == beat
        assert await _agent_events(repo, agent.id) == []

    @pytest.mark.asyncio
    async def test_repeated_heartbeats_stay_silent(self, repo, agent):
        for _ in range(50):
            await repo.update_agent(agent.id, last_active_at=utc_now())
        assert await _agent_events(repo, agent.id) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"status": "offline"},
            {"current_task": "x"},
            {"session_id": "s-1"},
            # A heartbeat riding along with a real state change still counts.
            {"status": "busy", "last_active_at": utc_now()},
            # Context watermarks are deliberately still eventful.
            {"ctx_tokens": 100, "ctx_pct": 0.5},
        ],
    )
    async def test_any_real_change_still_emits(self, repo, agent, changes):
        await repo.update_agent(agent.id, **changes)
        events = await _agent_events(repo, agent.id)
        assert len(events) == 1
        assert set(events[0].data["changes"]) == set(changes)


class TestDetectionIsUnaffected:
    """The user's hard condition, expressed as behaviour rather than a promise."""

    @pytest.mark.asyncio
    async def test_fresh_silent_heartbeat_still_spares_the_agent(self, repo, agent):
        reaper = StateReaper(repo=repo, event_bus=EventBus(repo=repo))
        await repo.update_agent(agent.id, status="busy")
        await repo.update_agent(agent.id, last_active_at=utc_now())

        reaped = await reaper._check_hook_agent(
            await repo.get_agent(agent.id), utc_now(), repo
        )

        assert reaped is False
        assert (await repo.get_agent(agent.id)).status.value == "busy"

    @pytest.mark.asyncio
    async def test_stale_heartbeat_is_still_reaped(self, repo, agent):
        reaper = StateReaper(repo=repo, event_bus=EventBus(repo=repo))
        await repo.update_agent(agent.id, status="busy")
        await repo.update_agent(
            agent.id, last_active_at=utc_now() - timedelta(minutes=30)
        )

        reaped = await reaper._check_hook_agent(
            await repo.get_agent(agent.id), utc_now(), repo
        )

        assert reaped is True
        assert (await repo.get_agent(agent.id)).status.value == "offline"

    @pytest.mark.asyncio
    async def test_wake_actionable_still_sees_the_heartbeat(self, repo, agent):
        """The 'just finished work from busy' criterion of `/api/wake/actionable` also reads this column."""
        from aiteam.api import wake_actionable

        beat = utc_now()
        await repo.update_agent(agent.id, status="waiting", last_active_at=beat)
        row = await repo.get_agent(agent.id)

        assert wake_actionable._after(row.last_active_at, beat - timedelta(minutes=1))
