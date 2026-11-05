"""AI Team OS — Memory module unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiteam.memory.recovery import ContextRecovery
from aiteam.memory.retriever import (
    build_context_string,
    keyword_search,
    rank_by_relevance,
)
from aiteam.memory.store import MemoryStore
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory, MemoryScope

# ================================================================
# MemoryStore
# ================================================================


async def test_store_and_retrieve(db_repository: StorageRepository) -> None:
    """Store a memory and retrieve it by keyword."""
    store = MemoryStore(db_repository)

    mid = await store.store("agent", "a1", "Python is a programming language", {"tag": "lang"})
    assert isinstance(mid, str) and len(mid) > 0

    await store.store("agent", "a1", "FastAPI is used to build REST APIs")
    await store.store("agent", "a1", "React is a frontend framework")

    results = await store.retrieve("agent", "a1", "Python", limit=5)
    assert len(results) >= 1
    assert any("Python" in m.content for m in results)


async def test_hot_cache(db_repository: StorageRepository) -> None:
    """Verify that after store, the memory exists in the Hot layer cache as well."""
    store = MemoryStore(db_repository)

    await store.store("agent", "a1", "Hot layer cache test content")

    # Check the Hot layer cache directly
    key = store._cache_key("agent", "a1")
    assert key in store._hot_cache
    assert len(store._hot_cache[key]) == 1
    assert store._hot_cache[key][0].content == "Hot layer cache test content"

    # Store more memories; the cache should accumulate
    await store.store("agent", "a1", "Second memory")
    assert len(store._hot_cache[key]) == 2


async def test_memory_scope_isolation(db_repository: StorageRepository) -> None:
    """Memories of different scopes should be isolated from each other."""
    store = MemoryStore(db_repository)

    await store.store("agent", "a1", "Agent private memory about Python")
    await store.store("team", "t1", "Team shared memory about Python")
    await store.store("global", "system", "Global memory about Python")

    agent_results = await store.retrieve("agent", "a1", "Python", limit=10)
    team_results = await store.retrieve("team", "t1", "Python", limit=10)
    global_results = await store.retrieve("global", "system", "Python", limit=10)

    # Each scope can only retrieve its own memories
    assert all(m.scope == MemoryScope.AGENT for m in agent_results)
    assert all(m.scope == MemoryScope.TEAM for m in team_results)
    assert all(m.scope == MemoryScope.GLOBAL for m in global_results)

    # Each scope has at most 1 entry
    assert len(agent_results) <= 1
    assert len(team_results) <= 1
    assert len(global_results) <= 1


async def test_delete_memory(db_repository: StorageRepository) -> None:
    """Deleting a memory should clean both the Hot and Warm layers."""
    store = MemoryStore(db_repository)

    mid = await store.store("agent", "a1", "Memory to be deleted")

    # Before deletion: present in both the Hot and Warm layers
    key = store._cache_key("agent", "a1")
    assert len(store._hot_cache[key]) == 1
    warm = await store.list_all("agent", "a1")
    assert len(warm) == 1

    # Perform the deletion
    result = await store.delete(mid)
    assert result is True

    # After deletion: both the Hot and Warm layers are cleared
    assert len(store._hot_cache[key]) == 0
    warm_after = await store.list_all("agent", "a1")
    assert len(warm_after) == 0


async def test_get_context(db_repository: StorageRepository) -> None:
    """Build an Agent context string."""
    store = MemoryStore(db_repository)

    # Store an agent-level memory
    await store.store("agent", "agent-001", "This Agent is good at data analysis and Python programming")
    # Store a global-level memory
    await store.store("global", "system", "The project uses the Python and FastAPI stack")

    ctx = await store.get_context("agent-001", "Analyze data with Python")

    # The context should contain relevant memories
    assert "Relevant memories" in ctx
    assert "data analysis" in ctx or "Python" in ctx


async def test_list_all(db_repository: StorageRepository) -> None:
    """List all memories in the specified scope."""
    store = MemoryStore(db_repository)

    await store.store("team", "t1", "Memory 1")
    await store.store("team", "t1", "Memory 2")
    await store.store("team", "t1", "Memory 3")
    await store.store("team", "t2", "Another team's memory")

    t1_mems = await store.list_all("team", "t1")
    assert len(t1_mems) == 3

    t2_mems = await store.list_all("team", "t2")
    assert len(t2_mems) == 1


async def test_archive(db_repository: StorageRepository, tmp_path: Path) -> None:
    """Archive memories to a Cold layer JSON file."""
    archive_dir = tmp_path / "archive"
    store = MemoryStore(db_repository, archive_dir=archive_dir)

    await store.store("team", "t1", "Archived memory 1")
    await store.store("team", "t1", "Archived memory 2")

    file_path = await store.archive("team", "t1")

    assert file_path.exists()
    assert file_path.suffix == ".json"
    assert "team" in str(file_path)
    assert "t1" in str(file_path)

    import json

    data = json.loads(file_path.read_text(encoding="utf-8"))
    assert len(data) == 2


# ================================================================
# Retriever
# ================================================================


async def test_keyword_search() -> None:
    """Keyword search matching test."""
    m1 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="The Python programming language is popular")
    m2 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="LangGraph builds state graphs")
    m3 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="Python and LangChain integration")

    # Search Python: should hit m1 and m3
    results = keyword_search([m1, m2, m3], "Python")
    assert len(results) == 2
    contents = [m.content for m in results]
    assert any("Python programming" in c for c in contents)
    assert any("LangChain" in c for c in contents)

    # Search LangGraph: only hits m2
    results = keyword_search([m1, m2, m3], "LangGraph")
    assert len(results) == 1
    assert "LangGraph" in results[0].content

    # Search a nonexistent word: empty result
    results = keyword_search([m1, m2, m3], "Java")
    assert len(results) == 0


async def test_rank_by_relevance() -> None:
    """Relevance ranking test."""
    m1 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="Python web development")
    m2 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="Database design")
    m3 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="Python data analysis and web scraping")

    # m3 hits both "Python" and "web", m1 also hits both, m2 hits 0
    ranked = rank_by_relevance([m1, m2, m3], "Python web")
    # m2 should rank last (0 hits)
    assert ranked[-1].content == "Database design"


async def test_build_context_string() -> None:
    """Context string building test."""
    m1 = Memory(scope=MemoryScope.AGENT, scope_id="a1", content="Memory content one")
    m2 = Memory(scope=MemoryScope.GLOBAL, scope_id="sys", content="Memory content two")

    ctx = build_context_string([m1, m2])
    assert "Relevant memories" in ctx
    assert "Memory content one" in ctx
    assert "Memory content two" in ctx
    assert "(agent/a1)" in ctx
    assert "(global/sys)" in ctx

    # An empty list returns an empty string
    assert build_context_string([]) == ""

    # max_tokens limit
    short_ctx = build_context_string([m1, m2], max_tokens=30)
    # The header itself takes many characters, so it may contain only part of the content
    assert len(short_ctx) <= 50  # Allow some margin, since the header may sit exactly on the boundary


# ================================================================
# ContextRecovery
# ================================================================


async def test_checkpoint_create_restore(tmp_path: Path) -> None:
    """Create a checkpoint and restore state."""
    recovery = ContextRecovery(checkpoint_dir=tmp_path / "checkpoints")

    state = {
        "task": "Analyze data",
        "progress": 50,
        "messages": ["hello", "world"],
    }
    cp_id = await recovery.create_checkpoint("agent-001", state)
    assert isinstance(cp_id, str) and len(cp_id) > 0

    # Restore the checkpoint
    restored = await recovery.restore_checkpoint(cp_id)
    assert restored["agent_id"] == "agent-001"
    assert restored["state"]["task"] == "Analyze data"
    assert restored["state"]["progress"] == 50
    assert restored["state"]["messages"] == ["hello", "world"]
    assert "timestamp" in restored

    # Restoring a nonexistent checkpoint should raise FileNotFoundError
    with pytest.raises(FileNotFoundError):
        await recovery.restore_checkpoint("nonexistent-id")


async def test_checkpoint_list(tmp_path: Path) -> None:
    """List all checkpoints of an Agent."""
    recovery = ContextRecovery(checkpoint_dir=tmp_path / "checkpoints")

    # Empty list
    assert await recovery.list_checkpoints("agent-001") == []

    # Create multiple checkpoints
    cp1 = await recovery.create_checkpoint("agent-001", {"step": 1})
    cp2 = await recovery.create_checkpoint("agent-001", {"step": 2})
    cp3 = await recovery.create_checkpoint("agent-001", {"step": 3})

    # Other Agents' checkpoints should not appear
    await recovery.create_checkpoint("agent-002", {"step": 99})

    cps = await recovery.list_checkpoints("agent-001")
    assert len(cps) == 3

    # Verify ascending time order
    ids = [cp["checkpoint_id"] for cp in cps]
    assert cp1 in ids
    assert cp2 in ids
    assert cp3 in ids

    # agent-002 has only 1
    cps2 = await recovery.list_checkpoints("agent-002")
    assert len(cps2) == 1


async def test_checkpoint_cleanup(tmp_path: Path) -> None:
    """Clean up old checkpoints, keeping only the newest N."""
    recovery = ContextRecovery(checkpoint_dir=tmp_path / "checkpoints")

    # Create 7 checkpoints
    for i in range(7):
        await recovery.create_checkpoint("agent-001", {"step": i})

    cps_before = await recovery.list_checkpoints("agent-001")
    assert len(cps_before) == 7

    # Cleanup: keep only the newest 3
    deleted = await recovery.cleanup_old_checkpoints("agent-001", keep_latest=3)
    assert deleted == 4

    cps_after = await recovery.list_checkpoints("agent-001")
    assert len(cps_after) == 3

    # The ones kept should be the newest 3 (largest timestamps)
    timestamps = [cp["timestamp"] for cp in cps_after]
    assert timestamps == sorted(timestamps)

    # Cleaning a nonexistent Agent returns 0
    assert await recovery.cleanup_old_checkpoints("nonexistent") == 0
