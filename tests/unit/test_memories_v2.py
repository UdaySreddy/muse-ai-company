"""Memory system v2 P1 / v2.1 — direction-layer activation unit tests.

Coverage: repository create_memory (kind/source_refs/supersedes), list_memories
valid-only + kind filtering, invalidate_memory, list_direction_memories assembly +
kind priority sorting, bucket character usage accounting, and the direction-memory rendering and injection fuse of both
hooks.
(pyproject asyncio_mode=auto; async tests need no decorator.)
"""

from __future__ import annotations

from aiteam.storage.repository import StorageRepository


async def test_create_memory_with_kind_and_source_refs(
    db_repository: StorageRepository,
) -> None:
    """create_memory persists kind/source_refs; the to_pydantic round-trip agrees."""
    m = await db_repository.create_memory(
        scope="global",
        scope_id="system",
        content="All output in Chinese",
        kind="constraint",
        source_refs=["report#abc", "task#123"],
    )
    assert m.kind == "constraint"
    assert m.source_refs == ["report#abc", "task#123"]
    assert m.invalid_at is None

    fetched = await db_repository.get_memory(m.id)
    assert fetched is not None
    assert fetched.kind == "constraint"
    assert fetched.source_refs == ["report#abc", "task#123"]


async def test_supersedes_invalidates_old(db_repository: StorageRepository) -> None:
    """supersedes marks the old entry invalid_at + invalidated_by; the default list excludes invalid entries."""
    old = await db_repository.create_memory(
        scope="global", scope_id="system", content="Old preference", kind="preference"
    )
    new = await db_repository.create_memory(
        scope="global",
        scope_id="system",
        content="New preference",
        kind="preference",
        supersedes=old.id,
    )

    valid = await db_repository.list_memories("global", "system")
    assert [m.id for m in valid] == [new.id]

    all_m = await db_repository.list_memories(
        "global", "system", include_invalidated=True
    )
    old_row = next(m for m in all_m if m.id == old.id)
    assert old_row.invalid_at is not None
    assert old_row.invalidated_by == new.id


async def test_invalidate_memory(db_repository: StorageRepository) -> None:
    """invalidate_memory explicitly invalidates without deleting; the entry disappears from the valid list."""
    m = await db_repository.create_memory(
        scope="user", scope_id="user", content="Outdated preference", kind="directive"
    )
    out = await db_repository.invalidate_memory(m.id, invalidated_by="manual")
    assert out is not None
    assert out.invalid_at is not None
    assert out.invalidated_by == "manual"

    valid = await db_repository.list_memories("user", "user")
    assert m.id not in [x.id for x in valid]
    # A nonexistent id returns None
    assert await db_repository.invalidate_memory("nope") is None


async def test_list_memories_kind_filter(db_repository: StorageRepository) -> None:
    """list_memories supports kind filtering."""
    await db_repository.create_memory(
        scope="global", scope_id="system", content="Constraint A", kind="constraint"
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="Preference B", kind="preference"
    )
    only = await db_repository.list_memories("global", "system", kind="constraint")
    assert [m.content for m in only] == ["Constraint A"]


async def test_list_direction_memories_scope_and_priority(
    db_repository: StorageRepository,
) -> None:
    """Direction-layer assembly: global+user global entries + the current project's project-level entries; sorted by
    kind priority."""
    proj = await db_repository.create_project("dir-proj", root_path="/tmp/dir-proj")
    other = await db_repository.create_project(
        "other-proj", root_path="/tmp/other-proj"
    )

    await db_repository.create_memory(
        scope="global", scope_id="system",
        content="pref-global", kind="preference",
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="constraint-global", kind="constraint"
    )
    await db_repository.create_memory(
        scope="user", scope_id="user", content="design-user", kind="design"
    )
    await db_repository.create_memory(
        scope="project", scope_id=proj.id, content="directive-proj", kind="directive"
    )
    # Project-level entries of other projects should not enter this project's injection
    await db_repository.create_memory(
        scope="project", scope_id=other.id, content="other-proj-entry", kind="constraint"
    )

    items = await db_repository.list_direction_memories(project_id=proj.id)
    contents = [m.content for m in items]
    assert "other-proj-entry" not in contents
    assert set(contents) == {
        "pref-global", "constraint-global", "design-user", "directive-proj"
    }
    # kind priority: constraint(0) < design(1) < directive(2) < preference(3)
    kinds = [m.kind for m in items]
    assert kinds == sorted(kinds, key=lambda k: {
        "constraint": 0, "design": 1, "directive": 2, "preference": 3
    }[k])
    assert kinds[0] == "constraint"


async def test_list_direction_memories_no_project(
    db_repository: StorageRepository,
) -> None:
    """Without project_id, only global+user are included; all project-level entries are excluded."""
    proj = await db_repository.create_project("p1")
    await db_repository.create_memory(
        scope="global", scope_id="system", content="g", kind="constraint"
    )
    await db_repository.create_memory(
        scope="project", scope_id=proj.id, content="p", kind="constraint"
    )
    items = await db_repository.list_direction_memories(project_id=None)
    assert [m.content for m in items] == ["g"]


async def test_bucket_valid_chars_only_counts_valid(
    db_repository: StorageRepository,
) -> None:
    """Bucket character usage accounting (the denominator of the v2.1 red line): invalid entries take no quota."""
    a = await db_repository.create_memory(
        scope="global", scope_id="system", content="A" * 30, kind="preference"
    )
    await db_repository.create_memory(
        scope="global", scope_id="system", content="B" * 20, kind="preference"
    )

    def used() -> int:
        return sum(len(m.content) for m in valid)

    valid = await db_repository.list_memories("global", "system")
    assert used() == 50
    await db_repository.invalidate_memory(a.id)
    valid = await db_repository.list_memories("global", "system")
    assert used() == 20


# ================================================================
# Direction-memory rendering functions of both hooks (stdlib only; unit-test the rendering logic directly)
# ================================================================


def test_render_direction_memories_bootstrap_truncation() -> None:
    """session_bootstrap rendering: over-budget entries are truncated in order with the remaining count noted."""
    from aiteam.hooks.session_bootstrap import _render_direction_memories

    items = [
        {"kind": "constraint", "content": "C" * 50},
        {"kind": "design", "content": "D" * 50},
        {"kind": "preference", "content": "P" * 50},
    ]
    lines = _render_direction_memories(items, budget=120)
    text = "\n".join(lines)
    assert "Directional memories" in text
    assert "Constraints / Guardrails" in text  # High priority is kept first
    assert "more entries" in text and "memory_list" in text  # Triggers the truncation hint


def test_render_direction_memories_empty() -> None:
    """An empty list renders empty (injects no noise)."""
    from aiteam.hooks.session_bootstrap import _render_direction_memories

    assert _render_direction_memories([]) == []


def test_render_direction_memories_subagent() -> None:
    """inject_subagent_context rendering: kind labels + contents all present; everything fits, no truncation."""
    from aiteam.hooks.inject_subagent_context import _render_direction_memories

    items = [
        {"kind": "constraint", "content": "All output in Chinese"},
        {"kind": "directive", "content": "Report as soon as done"},
    ]
    lines = _render_direction_memories(items)
    text = "\n".join(lines)
    assert "All output in Chinese" in text
    assert "Report as soon as done" in text
    assert "more entries" not in text  # Not truncated


def test_injection_fuse_covers_full_storage_quota() -> None:
    """The injection fuse ≥ the total storage quota (v2.1 single axis): what can be stored must be deliverable.

    Both hooks are pure-stdlib files with constants defined separately, so align each here against the sum of the
    server-side quotas —
    if the fuse drops below the quota, injection falls back to "routine truncation" and direction-layer inheritance is
    hollowed out again.
    """
    from aiteam.api.routes.memory import _DIRECTION_TOTAL_BUDGET
    from aiteam.hooks.inject_subagent_context import (
        _MEM_INJECT_FUSE as SUBAGENT_FUSE,
    )
    from aiteam.hooks.session_bootstrap import _MEM_INJECT_FUSE as BOOTSTRAP_FUSE

    assert SUBAGENT_FUSE == BOOTSTRAP_FUSE
    assert SUBAGENT_FUSE >= _DIRECTION_TOTAL_BUDGET


def test_full_quota_direction_layer_injects_without_truncation() -> None:
    """A full-quota (3000-character) direction layer injected whole, with no "N more entries" truncation tail."""
    from aiteam.hooks.inject_subagent_context import _render_direction_memories

    # 30 entries × 100 characters = 3000 characters, exactly the sum of the three buckets' quotas
    items = [
        {"kind": "constraint", "content": f"Entry {i:02d}" + "A" * 95}
        for i in range(30)
    ]
    text = "\n".join(_render_direction_memories(items))
    assert "more entries" not in text
    assert "Entry 29" in text
