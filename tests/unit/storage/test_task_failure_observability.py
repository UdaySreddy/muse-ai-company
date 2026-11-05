"""Failure-observability write-side regression tests — closing the observability broken chain, batch (1).

Broken-chain record (2026-07-28 forensics): since its declaration, ``EventType.TASK_FAILED`` has had no **living**
writer —

* ``config/settings.py``'s notification event whitelist references ``task.failed``;
* ``api/routes/settings.py``'s default notification configuration also lists it;
* In ``orchestrator/team_manager.py`` there is indeed one ``_emit("task.failed", ...)``,
  but it hangs on ``event_bus`` and that module has no importers anywhere in the repo — dead code from the retired
  orchestration path.

So the fact 'a task failed' never appeared in the event stream at all, and failure observability relied entirely on
agents reporting themselves;
when an agent is stuck / killed / silently turns failed, the system knows nothing about the failure.

This group of tests pins ``task.failed`` to **the single entry point of the storage-layer state machine**,
``StorageRepository.update_task``: as long as the status is changed to failed, no matter whether the caller is the API,
an MCP tool, reaper collection, or a direct repo call, the event must be persisted, carrying the previous status and the
failure
context — without relying on any caller's conscientiousness.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repo for isolation."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _make_task(repo: StorageRepository, title: str = "Observability broken-chain closure verification task"):
    """Create a team + task and return the task."""
    team = await repo.create_team(name="d1-observability", mode="coordinate")
    return await repo.create_task(team.id, title=title, description="Failure-observability write-side verification")


async def _failed_events(repo: StorageRepository, task_id: str) -> list:
    """Fetch all task.failed events for one task."""
    events = await repo.list_events(event_type="task.failed", limit=100)
    return [e for e in events if e.entity_id == task_id]


# ---------------------------------------------------------------------------
# Core: transitioning the state machine to failed must record an event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_to_failed_emits_task_failed(repo: StorageRepository) -> None:
    """The status changes to failed → the storage layer must automatically record a task.failed (not relying on the
    caller to report itself)."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)

    await repo.update_task(task.id, status="failed", result="The worker process was killed, with no self-report")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, "Transitioning to failed recorded no task.failed event — the observability chain is broken"
    assert hits[0].entity_type == "task"
    assert hits[0].source == "repository"


@pytest.mark.asyncio()
async def test_task_failed_carries_prior_status_and_context(repo: StorageRepository) -> None:
    """The event must carry the previous status and the failure context, otherwise it cannot be attributed
    afterwards."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)
    await repo.update_task(task.id, status="failed", result="The dependent API timed out 3 times in a row")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1
    data = hits[0].data

    assert data["task_id"] == task.id
    # The previous status: which status the failure fell from is the first-hand information for attribution
    assert data["from_status"] == "running", f"The previous status was lost: {data}"
    assert data["to_status"] == "failed"
    # Failure context
    assert "timed out" in (data.get("failure_context") or ""), f"The failure context was lost: {data}"
    assert data["team_id"] == task.team_id
    # The snapshot is still given by update_task's existing convention
    assert hits[0].state_snapshot["status"] == "failed"


@pytest.mark.asyncio()
async def test_enum_status_input_also_emits(repo: StorageRepository) -> None:
    """When the caller passes a TaskStatus enum (team_manager's old writing style), an event must likewise be
    recorded."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.FAILED, result="Enum-argument path")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, "The enum-argument path was missed"


@pytest.mark.asyncio()
async def test_repeated_failed_update_does_not_duplicate(repo: StorageRepository) -> None:
    """Emit only once, on 'transitioning into' the failed state; writing failed repeatedly does not flood (the event
    stream is a ledger, not a log)."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="First failure")
    await repo.update_task(task.id, result="Adding an explanatory note")  # Status unchanged
    await repo.update_task(task.id, status="failed", result="Repeatedly writing the same status")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, f"Repeatedly transitioning to failed flooded the events: {len(hits)} entries"


@pytest.mark.asyncio()
async def test_non_failure_transition_emits_nothing(repo: StorageRepository) -> None:
    """Normal transitions must not falsely report failure."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)
    await repo.update_task(task.id, status=TaskStatus.COMPLETED.value, result="Completed smoothly")

    assert await _failed_events(repo, task.id) == []


@pytest.mark.asyncio()
async def test_recovery_then_fail_again_emits_second_event(repo: StorageRepository) -> None:
    """failed → retry running → failed again: the second failure is a new fact and must record another event."""
    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="First failure")
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)  # Retry
    await repo.update_task(task.id, status="failed", result="Second failure")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 2, f"A failure after a retry was missed: {len(hits)} entries"
    assert {h.data["from_status"] for h in hits} == {"pending", "running"}


# ---------------------------------------------------------------------------
# Diagnostic results must likewise record events: if the analysis was done but left no trace, it is as if it was not
# done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_failure_analysis_lands_event(repo: StorageRepository) -> None:
    """After failure_analysis (failure alchemy) runs, it must record task.failure_analyzed."""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="Connection refused")

    await FailureAlchemist(repo).process_failure(task.id, task.team_id)

    events = await repo.list_events(event_type="task.failure_analyzed", limit=50)
    hits = [e for e in events if e.entity_id == task.id]
    assert len(hits) == 1, "The failure-alchemy result recorded no event"
    assert hits[0].data["task_id"] == task.id
    # The product summary must be able to prove the analysis really produced something
    assert hits[0].data["artifacts"] == ["antibody", "vaccine", "catalyst"]


@pytest.mark.asyncio()
async def test_diagnose_failure_lands_event(repo: StorageRepository) -> None:
    """After diagnose_task_failure runs, it must record task.failure_diagnosed, carrying the root cause."""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="Database connection pool exhausted")

    await FailureAlchemist(repo).diagnose_failure(task.id)

    events = await repo.list_events(event_type="task.failure_diagnosed", limit=50)
    hits = [e for e in events if e.entity_id == task.id]
    assert len(hits) == 1, "The failure-diagnosis result recorded no event"
    assert "connection pool" in hits[0].data["root_cause"]


@pytest.mark.asyncio()
async def test_diagnosis_on_missing_task_lands_nothing(repo: StorageRepository) -> None:
    """When the task does not exist, no empty event must be recorded."""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    await FailureAlchemist(repo).diagnose_failure("no-such-task")
    assert await repo.list_events(event_type="task.failure_diagnosed", limit=50) == []
