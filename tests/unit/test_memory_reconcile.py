"""Memory system v2 P2 — on-demand tidy-up memory_reconcile unit tests.

Coverage: reconcile coarse-filter clustering + BM25 pairing (build_candidate_groups), repository
new methods (list_project_task_memos / invalidate_task_memo idempotency / score_task_memo /
count_valid_task_memos_since / get+set_last_reconcile_at）。
(pyproject asyncio_mode=auto; async tests need no decorator.)
"""

from __future__ import annotations

from datetime import timedelta

from aiteam.clock import utc_now
from aiteam.memory.reconcile import build_candidate_groups
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskMemo

# ================================================================
# Coarse filter: clustering + BM25 pairwise pairing (pure functions, no DB dependency)
# ================================================================


def _memo(content: str, task_id: str = "t1", scope_path: str = "") -> TaskMemo:
    return TaskMemo(task_id=task_id, content=content, scope_path=scope_path)


def test_similar_memos_pair_into_group() -> None:
    """Two highly similar entries in the same scope_path → form a candidate group containing the full text."""
    memos = [
        _memo("Deploy the API to production using the docker compose command", scope_path="/deploy"),
        _memo("Production deployment of the API started with the docker compose command", scope_path="/deploy"),
        _memo("Frontend React component style tweaks and color replacements", scope_path="/deploy"),
    ]
    groups = build_candidate_groups(memos, threshold=0.45)
    assert len(groups) == 1
    group = groups[0]
    assert group["member_count"] == 2
    contents = {m["content"] for m in group["members"]}
    assert any("Deploy the API" in c for c in contents)
    # The unrelated frontend memo should not enter the group
    assert all("Frontend React" not in c for c in contents)


def test_dissimilar_memos_no_group() -> None:
    """No two entries in the cluster are similar → no candidate group is produced."""
    memos = [
        _memo("Database index optimization of query performance", scope_path="/db"),
        _memo("Writing the user manual documentation", scope_path="/db"),
    ]
    assert build_candidate_groups(memos, threshold=0.45) == []


def test_empty_scope_clusters_by_task_not_globally() -> None:
    """Similar memos without scope_path belong to different tasks → no cross-task clustering into a group."""
    memos = [
        _memo("Fix the null pointer exception in the login endpoint", task_id="task-A"),
        _memo("Fix the null pointer exception in the login endpoint", task_id="task-B"),
    ]
    # Different tasks + empty scope_path → different cluster_key → no group
    assert build_candidate_groups(memos, threshold=0.3) == []


def test_promotion_candidate_flagged_across_tasks() -> None:
    """The same scope_path recurring across tasks → marked promotion_candidate."""
    memos = [
        _memo("The user requires all output in Chinese", task_id="task-A", scope_path="/pref"),
        _memo("The user requires all output to reply in Chinese", task_id="task-B", scope_path="/pref"),
    ]
    groups = build_candidate_groups(memos, threshold=0.4)
    assert len(groups) == 1
    assert groups[0]["promotion_candidate"] is True
    assert groups[0]["distinct_tasks"] == 2


# ================================================================
# New repository methods
# ================================================================


async def test_list_project_task_memos(db_repository: StorageRepository) -> None:
    """List valid memos by project; invalid / other-project entries are excluded."""
    await db_repository.add_task_memo("t1", content="a", project_id="P1")
    m2 = await db_repository.add_task_memo("t1", content="b", project_id="P1")
    await db_repository.add_task_memo("t2", content="c", project_id="P2")
    await db_repository.invalidate_task_memo(m2.id)

    listed = await db_repository.list_project_task_memos("P1")
    contents = [m.content for m in listed]
    assert contents == ["a"]  # b is invalid, c belongs to P2


async def test_list_project_task_memos_scope_path(
    db_repository: StorageRepository,
) -> None:
    """When scope_path is given → only that scope is taken."""
    await db_repository.add_task_memo(
        "t1", content="x", project_id="P1", scope_path="/deploy"
    )
    await db_repository.add_task_memo(
        "t1", content="y", project_id="P1", scope_path="/db"
    )
    only = await db_repository.list_project_task_memos("P1", scope_path="/deploy")
    assert [m.content for m in only] == ["x"]


async def test_invalidate_task_memo_idempotent(
    db_repository: StorageRepository,
) -> None:
    """Repeated invalidation does not overwrite the first invalidated_by (idempotency base)."""
    m = await db_repository.add_task_memo("t1", content="a", project_id="P1")
    first = await db_repository.invalidate_task_memo(m.id, invalidated_by="new-1")
    assert first is not None and first.invalidated_by == "new-1"
    at1 = first.invalid_at
    # Second invalidation: does not overwrite
    second = await db_repository.invalidate_task_memo(m.id, invalidated_by="new-2")
    assert second is not None
    assert second.invalidated_by == "new-1"
    assert second.invalid_at == at1
    # Nonexistent → None
    assert await db_repository.invalidate_task_memo("nope") is None


async def test_score_task_memo(db_repository: StorageRepository) -> None:
    """Scoring enqueues; reason goes into meta; nonexistent id → None."""
    m = await db_repository.add_task_memo("t1", content="Summary", project_id="P1")
    scored = await db_repository.score_task_memo(m.id, 8, "High information density")
    assert scored is not None
    assert scored.quality_score == 8
    assert scored.meta.get("quality_reason") == "High information density"
    assert await db_repository.score_task_memo("nope", 5) is None


async def test_count_valid_task_memos_since(
    db_repository: StorageRepository,
) -> None:
    """The valid count after since; None counts all."""
    old = await db_repository.add_task_memo("t1", content="old", project_id="P1")
    # Manually move old's created_at into the past, simulating "before the last tidy-up"
    cutoff = utc_now()
    await db_repository.add_task_memo("t1", content="new", project_id="P1")

    total = await db_repository.count_valid_task_memos_since("P1", None)
    assert total == 2
    since = await db_repository.count_valid_task_memos_since("P1", cutoff)
    assert since == 1  # Only new, after the cutoff
    assert old.content == "old"


async def test_last_reconcile_at_roundtrip(
    db_repository: StorageRepository,
) -> None:
    """set/get last_reconcile_at round-trips through project.config consistently."""
    project = await db_repository.create_project(name="P")
    assert await db_repository.get_last_reconcile_at(project.id) is None
    when = utc_now() - timedelta(minutes=5)
    out = await db_repository.set_last_reconcile_at(project.id, when)
    assert out == when
    got = await db_repository.get_last_reconcile_at(project.id)
    assert got is not None
    assert abs((got - when).total_seconds()) < 1
    # Project does not exist → None
    assert await db_repository.set_last_reconcile_at("nope") is None
