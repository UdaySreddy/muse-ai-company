"""Memory system v2 P0 — task_memos table promotion unit tests.

Coverage: repository memo CRUD, supersedes replacement invalidation, config→table backfill idempotency,
config['memo'] view hydration, unified_search filtering of invalid entries.
(pyproject asyncio_mode=auto; async tests need no decorator.)
"""

from __future__ import annotations

from aiteam.api.unified_search import unified_search
from aiteam.storage.repository import StorageRepository


async def test_add_and_list_task_memos(db_repository: StorageRepository) -> None:
    """add_task_memo writes the table; list_task_memos returns valid entries in time order."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "Task A")

    m1 = await db_repository.add_task_memo(task.id, "First", memo_type="progress")
    m2 = await db_repository.add_task_memo(
        task.id, "Second", memo_type="summary", author="engineer-1"
    )

    memos = await db_repository.list_task_memos(task.id)
    assert [m.content for m in memos] == ["First", "Second"]
    assert memos[0].id == m1.id
    assert memos[1].id == m2.id
    assert memos[1].memo_type == "summary"
    assert memos[1].author == "engineer-1"
    assert all(m.invalid_at is None for m in memos)


async def test_get_task_memo(db_repository: StorageRepository) -> None:
    """get_task_memo fetches a single entry by its real id."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "Task B")
    m = await db_repository.add_task_memo(task.id, "Content X")

    fetched = await db_repository.get_task_memo(m.id)
    assert fetched is not None
    assert fetched.content == "Content X"
    assert await db_repository.get_task_memo("nonexistent") is None


async def test_supersedes_invalidation(db_repository: StorageRepository) -> None:
    """supersedes marks the old entry invalid_at + invalidated_by; the default list excludes invalid entries."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "Task C")

    old = await db_repository.add_task_memo(task.id, "Old conclusion", memo_type="decision")
    new = await db_repository.add_task_memo(
        task.id, "New conclusion", memo_type="decision", supersedes=old.id
    )

    valid = await db_repository.list_task_memos(task.id)
    assert [m.id for m in valid] == [new.id]

    all_memos = await db_repository.list_task_memos(task.id, include_invalidated=True)
    assert len(all_memos) == 2
    old_row = next(m for m in all_memos if m.id == old.id)
    assert old_row.invalid_at is not None
    assert old_row.invalidated_by == new.id


async def test_hydrate_config_memo_view(db_repository: StorageRepository) -> None:
    """get_task splices the valid memos back into the config['memo'] view (legacy read side compatible with zero
    changes)."""
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(team.id, "Task D")
    await db_repository.add_task_memo(task.id, "Progress one")

    fetched = await db_repository.get_task(task.id)
    assert fetched is not None
    view = fetched.config.get("memo", [])
    assert len(view) == 1
    assert view[0]["content"] == "Progress one"
    assert "id" in view[0]
    assert "timestamp" in view[0]


async def test_backfill_idempotent(db_repository: StorageRepository) -> None:
    """config.memo → table backfill idempotency: running twice imports no duplicates."""
    team = await db_repository.create_team("memo-team", "coordinate")
    legacy_config = {
        "memo": [
            {
                "timestamp": "2026-07-01T10:00:00",
                "author": "leader",
                "content": "Legacy memo 1",
                "type": "progress",
            },
            {
                "timestamp": "2026-07-01T11:00:00",
                "author": "engineer-2",
                "content": "Legacy memo 2",
                "type": "summary",
            },
        ]
    }
    task = await db_repository.create_task(team.id, "Old task", config=legacy_config)

    first = await db_repository.backfill_task_memos_from_config()
    assert first == 2

    second = await db_repository.backfill_task_memos_from_config()
    assert second == 0

    memos = await db_repository.list_task_memos(task.id)
    assert len(memos) == 2
    assert [m.content for m in memos] == ["Legacy memo 1", "Legacy memo 2"]
    assert memos[1].memo_type == "summary"
    assert memos[1].author == "engineer-2"


async def test_unified_search_filters_invalidated(
    db_repository: StorageRepository,
) -> None:
    """unified_search queries the table directly; invalid memos do not appear in results, and the superseder is
    retrievable."""
    project = await db_repository.create_project("proj-memo")
    team = await db_repository.create_team("memo-team", "coordinate")
    task = await db_repository.create_task(
        team.id, "Search task", project_id=project.id
    )

    old = await db_repository.add_task_memo(
        task.id, "Old unique-word zebra conclusion", memo_type="decision"
    )
    new = await db_repository.add_task_memo(
        task.id, "New unique-word zebra conclusion", memo_type="decision", supersedes=old.id
    )

    results = await unified_search(db_repository, "zebra", project_id=project.id)
    memo_ids = {r["id"] for r in results if r["kind"] == "task_memo"}
    assert new.id in memo_ids
    assert old.id not in memo_ids
