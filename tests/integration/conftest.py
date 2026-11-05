"""Integration tests — pytest fixtures.

Each test uses an independent temporary SQLite database and runs end-to-end tests through the FastAPI TestClient.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def integration_client(tmp_path, monkeypatch):
    """Create an integration test client, with an independent in-memory SQLite database per test."""
    # Config-file isolation: team_config's CONFIG_FILE is a module constant pointing at a real file in the repo;
    # tests hitting the real API once wrote 'integ'/fixer test data into the checked-in
    # plugin/config/team-defaults.json (recorded 2026-07-13) — redirect it to tmp
    from aiteam.api.routes import team_config

    monkeypatch.setattr(team_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(team_config, "CONFIG_FILE", tmp_path / "team-defaults.json")

    # Initialize an independent storage layer
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    # Inject into the deps module (including EventBus and TaskWallEngine)
    event_bus = EventBus(repo=repo)
    task_wall_engine = TaskWallEngine(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._task_wall_engine = task_wall_engine

    app = create_app()

    # Override the lifespan: no automatic init/cleanup is needed in tests
    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    # Cleanup
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._task_wall_engine = None


@pytest.fixture()
def repo_and_client(integration_client):
    """Provide both the repository and the client, for tests that need to manipulate the database directly."""
    return deps._repository, integration_client
