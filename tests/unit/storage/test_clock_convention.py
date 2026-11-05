"""Time-window conventions — 'the last N days' must really be N days.

Historical record: this database once ran two wall clocks simultaneously (the core domain local / the ecosystem domain
UTC), while SQLite silently
datetime's offset is silently stripped, so cross-domain comparisons are off by 8 hours **without raising**.
``aggregate_model_usage`` is one place that was bitten: the cutoff took UTC while the ``workflow_agents.updated_at`` it
compares
was written in local time, so 'the last N days' actually counted N days + 8 hours.

After the whole database was unified on UTC (see docs/utc-unification-design.md), both sides share the same source;
these two tests remain as a
regression net: the moment a window boundary is stretched or narrowed again by some clock difference, they turn red
immediately.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest.mark.asyncio()
async def test_usage_window_does_not_leak_eight_extra_hours(repo: StorageRepository) -> None:
    """A row just past the window must fall outside the window.

    This row is aged 7 days and 4 hours, only 4 hours beyond the window — smaller than any timezone offset. So as long
    as
    the cutoff and the column's convention diverge again by even one offset, it will be wrongly counted in.
    """
    from sqlalchemy import select

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import WorkflowAgentModel

    await repo.upsert_workflow_agent(
        WorkflowAgent(run_id="run-clock", wf_id="wf-clock", cc_agent_id="agent-stale", model="opus", tokens=999)
    )
    # Push the watermark beyond the window (with the same clock as that column: the database-wide unique UTC)
    async with get_session(repo._db_url) as session:
        row = (
            await session.execute(
                select(WorkflowAgentModel).where(WorkflowAgentModel.cc_agent_id == "agent-stale")
            )
        ).scalar_one()
        row.updated_at = utc_now() - timedelta(days=7, hours=4)

    rows = await repo.aggregate_model_usage(days=7)
    total = sum(r.get("agents", 0) for r in rows)
    assert total == 0, (
        f"Rows outside the window were counted in — the cutoff and the column use inconsistent conventions: {rows}"
    )


@pytest.mark.asyncio()
async def test_recent_row_still_counted(repo: StorageRepository) -> None:
    """A fix must not injure rows inside the window by mistake."""
    await repo.upsert_workflow_agent(
        WorkflowAgent(run_id="run-clock", wf_id="wf-clock", cc_agent_id="agent-fresh", model="opus", tokens=1234)
    )
    rows = await repo.aggregate_model_usage(days=7)
    assert sum(r.get("agents", 0) for r in rows) == 1
