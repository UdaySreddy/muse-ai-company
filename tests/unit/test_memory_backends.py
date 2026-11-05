"""AI Team OS — memory backend unit tests."""

from __future__ import annotations

from aiteam.memory.backends import MemoryBackend
from aiteam.memory.backends.sqlite_backend import SqliteMemoryBackend
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory, MemoryScope

# ================================================================
# SqliteMemoryBackend
# ================================================================


async def test_sqlite_backend_create(db_repository: StorageRepository) -> None:
    """The SQLite backend creates a memory."""
    backend = SqliteMemoryBackend(db_repository)
    memory = await backend.create("agent", "a1", "Test memory content", {"tag": "test"})

    assert isinstance(memory, Memory)
    assert memory.scope == MemoryScope.AGENT
    assert memory.scope_id == "a1"
    assert memory.content == "Test memory content"
    assert memory.metadata == {"tag": "test"}


async def test_sqlite_backend_search(db_repository: StorageRepository) -> None:
    """The SQLite backend searches memories."""
    backend = SqliteMemoryBackend(db_repository)
    await backend.create("agent", "a1", "The Python programming language")
    await backend.create("agent", "a1", "The Java virtual machine")
    await backend.create("agent", "a1", "Python data analysis")

    results = await backend.search("agent", "a1", "Python", limit=5)
    assert len(results) >= 1
    assert all("Python" in m.content for m in results)


async def test_sqlite_backend_list(db_repository: StorageRepository) -> None:
    """The SQLite backend lists all memories."""
    backend = SqliteMemoryBackend(db_repository)
    await backend.create("team", "t1", "Memory A")
    await backend.create("team", "t1", "Memory B")
    await backend.create("team", "t2", "Another team's memory")

    t1_list = await backend.list_all("team", "t1")
    assert len(t1_list) == 2

    t2_list = await backend.list_all("team", "t2")
    assert len(t2_list) == 1


async def test_sqlite_backend_get(db_repository: StorageRepository) -> None:
    """The SQLite backend gets a memory by ID."""
    backend = SqliteMemoryBackend(db_repository)
    created = await backend.create("agent", "a1", "Get test")

    fetched = await backend.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.content == "Get test"

    # A nonexistent ID returns None
    assert await backend.get("nonexistent-id") is None


async def test_sqlite_backend_delete(db_repository: StorageRepository) -> None:
    """The SQLite backend deletes a memory."""
    backend = SqliteMemoryBackend(db_repository)
    created = await backend.create("agent", "a1", "To be deleted")

    result = await backend.delete(created.id)
    assert result is True

    # Getting it after deletion should return None
    assert await backend.get(created.id) is None

    # Deleting a nonexistent ID returns False
    assert await backend.delete("nonexistent-id") is False


async def test_sqlite_backend_implements_protocol(
    db_repository: StorageRepository,
) -> None:
    """The SQLite backend should satisfy the MemoryBackend Protocol."""
    backend = SqliteMemoryBackend(db_repository)
    assert isinstance(backend, MemoryBackend)
