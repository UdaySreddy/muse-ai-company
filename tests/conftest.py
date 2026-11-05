"""AI Team OS — global pytest fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Provide a usable event loop as a fallback for sync tests (Python 3.12 + pytest-asyncio).

    pytest-asyncio closes and clears the MainThread loop after async tests finish,
    so a sync test queued afterwards that calls the legacy asyncio.get_event_loop().run_until_complete
    would raise RuntimeError (cross-test pollution: passes alone, fails in a full run).
    """
    try:
        closed = asyncio.get_event_loop().is_closed()
    except RuntimeError:
        closed = True
    if closed:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture()
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a temporary directory to use as the project directory."""
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()
    aiteam_dir = project_dir / ".aiteam"
    aiteam_dir.mkdir()
    return project_dir


@pytest_asyncio.fixture()
async def db_repository() -> StorageRepository:
    """Create a StorageRepository instance backed by in-memory SQLite.

    Uses an sqlite+aiosqlite:// in-memory database, automatically cleaned up after the test.
    """
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    yield repo  # type: ignore[misc]
    await close_db()
