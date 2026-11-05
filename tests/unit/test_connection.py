"""Tests database connection management — PostgreSQL connection pool parameter validation + EnginePool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiteam.storage.connection import get_engine
from aiteam.storage.engine_pool import EnginePool, engine_pool


@pytest.fixture(autouse=True)
def _reset_engine_pool():
    """Reset the global EnginePool cache before and after each test."""
    original_engines = engine_pool._engines.copy()
    original_factories = engine_pool._factories.copy()
    engine_pool._engines.clear()
    engine_pool._factories.clear()
    yield
    engine_pool._engines.clear()
    engine_pool._factories.clear()
    engine_pool._engines.update(original_engines)
    engine_pool._factories.update(original_factories)


@pytest.fixture(autouse=True)
def _patch_sqlalchemy_event():
    """Patch sqlalchemy.event.listens_for to a no-op decorator.

    engine_pool.py attaches a WAL pragma listener via event.listens_for on
    engine.sync_engine.  In tests, create_async_engine is mocked, so
    sync_engine is a MagicMock that the real SQLAlchemy event system cannot
    accept.  This fixture prevents the registration from running at all.
    """
    def _noop_listens_for(target, identifier, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    with patch("sqlalchemy.event.listens_for", side_effect=_noop_listens_for):
        yield


class TestGetEnginePoolConfig:
    """Verify get_engine() passes the correct parameters for different database URLs."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_sqlite_uses_check_same_thread(self, mock_create: MagicMock) -> None:
        """A SQLite URL should pass check_same_thread=False."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "sqlite+aiosqlite:///test.db"

        get_engine("sqlite+aiosqlite:///test.db")

        mock_create.assert_called_once()
        kwargs = mock_create.call_args
        assert kwargs[1]["connect_args"] == {"check_same_thread": False, "timeout": 30}
        # SQLite should not have connection pool parameters
        assert "pool_size" not in kwargs[1]
        assert "max_overflow" not in kwargs[1]

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_postgresql_uses_pool_config(self, mock_create: MagicMock) -> None:
        """A PostgreSQL URL should pass connection pool parameters."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "postgresql+asyncpg://user:pass@localhost/db"

        get_engine("postgresql+asyncpg://user:pass@localhost/db")

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["pool_size"] == 10
        assert kwargs["max_overflow"] == 20
        assert kwargs["pool_pre_ping"] is True
        assert kwargs["pool_recycle"] == 3600
        # PostgreSQL should not have SQLite connect_args
        assert "connect_args" not in kwargs

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_postgresql_no_check_same_thread(self, mock_create: MagicMock) -> None:
        """A PostgreSQL URL should not contain the check_same_thread parameter."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"

        get_engine("postgresql+asyncpg://localhost/db")

        kwargs = mock_create.call_args[1]
        assert "connect_args" not in kwargs

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_echo_always_false(self, mock_create: MagicMock) -> None:
        """The echo parameter of all engines should be False."""
        mock_create.return_value = MagicMock()

        # SQLite
        mock_create.return_value.url = "sqlite+aiosqlite:///test.db"
        get_engine("sqlite+aiosqlite:///test.db")
        assert mock_create.call_args[1]["echo"] is False

        # Reset the engine cache
        engine_pool._engines.clear()
        engine_pool._factories.clear()

        # PostgreSQL
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"
        get_engine("postgresql+asyncpg://localhost/db")
        assert mock_create.call_args[1]["echo"] is False


class TestGetEngineCache:
    """Verify the engine caching mechanism — EnginePool."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_same_url_returns_cached_engine(self, mock_create: MagicMock) -> None:
        """The same URL should return the cached engine instance."""
        mock_engine = MagicMock()
        mock_engine.url = "sqlite+aiosqlite:///test.db"
        mock_create.return_value = mock_engine

        engine1 = get_engine("sqlite+aiosqlite:///test.db")
        engine2 = get_engine("sqlite+aiosqlite:///test.db")

        assert engine1 is engine2
        assert mock_create.call_count == 1

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_different_url_creates_new_engine(self, mock_create: MagicMock) -> None:
        """Different URLs should create new engines."""
        mock_engine1 = MagicMock()
        mock_engine1.url = "sqlite+aiosqlite:///test1.db"
        mock_engine2 = MagicMock()
        mock_engine2.url = "sqlite+aiosqlite:///test2.db"
        mock_create.side_effect = [mock_engine1, mock_engine2]

        get_engine("sqlite+aiosqlite:///test1.db")
        get_engine("sqlite+aiosqlite:///test2.db")

        assert mock_create.call_count == 2


class TestEnginePoolEviction:
    """Verify the EnginePool LRU eviction mechanism."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_evicts_oldest_when_full(self, mock_create: MagicMock) -> None:
        """When max_size is exceeded, the oldest engine should be evicted."""
        pool = EnginePool(max_size=2)

        engines = []
        for i in range(3):
            mock_eng = MagicMock()
            mock_eng.url = f"sqlite+aiosqlite:///test{i}.db"
            engines.append(mock_eng)
        mock_create.side_effect = engines

        pool.get_engine("sqlite+aiosqlite:///test0.db")
        pool.get_engine("sqlite+aiosqlite:///test1.db")
        pool.get_engine("sqlite+aiosqlite:///test2.db")

        # Pool should only have 2 engines (test1 and test2)
        assert len(pool._engines) == 2
        assert "sqlite+aiosqlite:///test0.db" not in pool._engines
        assert "sqlite+aiosqlite:///test1.db" in pool._engines
        assert "sqlite+aiosqlite:///test2.db" in pool._engines
        # Evicted engine should be in pending dispose
        assert len(pool._pending_dispose) == 1
