"""Tests database path pinning — verifies the default path points to ~/.claude/data/ai-team-os/aiteam.db."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aiteam.storage.connection import _default_db_url, _migrate_old_db_if_needed


class TestDefaultDbUrl:
    """Verify _default_db_url() returns the correct fixed path."""

    def test_url_contains_expected_path(self, tmp_path: Path) -> None:
        """The returned URL should contain .claude/data/ai-team-os/aiteam.db."""
        # Patch home consistently with the rest of this class to avoid mkdir/migration triggers under the real ~/.claude
        with patch.object(Path, "home", return_value=tmp_path):
            url = _default_db_url()
        assert "sqlite+aiosqlite:///" in url
        assert ".claude" in url
        assert "data" in url
        assert "ai-team-os" in url
        assert "aiteam.db" in url

    def test_directory_auto_created(self, tmp_path: Path) -> None:
        """Use a mocked home directory to verify automatic directory creation."""
        fake_home = tmp_path / "fakehome"
        # Do not create the directory beforehand; verify the function creates it automatically
        with patch.object(Path, "home", return_value=fake_home):
            url = _default_db_url()

        expected_dir = fake_home / ".claude" / "data" / "ai-team-os"
        assert expected_dir.is_dir()
        assert url == f"sqlite+aiosqlite:///{expected_dir / 'aiteam.db'}"

    def test_idempotent_on_existing_directory(self, tmp_path: Path) -> None:
        """No error when the directory already exists."""
        fake_home = tmp_path / "fakehome"
        data_dir = fake_home / ".claude" / "data" / "ai-team-os"
        data_dir.mkdir(parents=True)

        with patch.object(Path, "home", return_value=fake_home):
            url = _default_db_url()

        assert "aiteam.db" in url
        assert data_dir.is_dir()


class TestDbPathEnvOverride:
    """Verify the AITEAM_DB_PATH environment variable override: when set, use that path and skip the old-database
    migration."""

    def test_env_override_used_and_skips_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AITEAM_DB_PATH is set, the URL points to that path and does not trigger old-database migration."""
        target = tmp_path / "custom" / "demo.db"
        monkeypatch.setenv("AITEAM_DB_PATH", str(target))

        with patch(
            "aiteam.storage.connection._migrate_old_db_if_needed"
        ) as mock_migrate:
            url = _default_db_url()

        assert url == f"sqlite+aiosqlite:///{target}"
        # The override path should not touch the old-database migration logic
        mock_migrate.assert_not_called()
        # The parent directory should be created automatically
        assert target.parent.is_dir()

    def test_env_override_expands_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ~ in AITEAM_DB_PATH should be expanded to the home directory."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AITEAM_DB_PATH", "~/demo-data/aiteam.db")

        url = _default_db_url()

        expected = tmp_path / "demo-data" / "aiteam.db"
        assert url == f"sqlite+aiosqlite:///{expected}"
        assert expected.parent.is_dir()

    def test_unset_falls_back_to_default_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AITEAM_DB_PATH is not set, behavior is exactly as before (fixed default path)."""
        monkeypatch.delenv("AITEAM_DB_PATH", raising=False)

        with patch.object(Path, "home", return_value=tmp_path):
            url = _default_db_url()

        expected_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        assert url == f"sqlite+aiosqlite:///{expected_dir / 'aiteam.db'}"
        assert expected_dir.is_dir()

    def test_empty_env_override_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty AITEAM_DB_PATH is treated as unset and falls back to the default path."""
        monkeypatch.setenv("AITEAM_DB_PATH", "")

        with patch.object(Path, "home", return_value=tmp_path):
            url = _default_db_url()

        expected_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        assert url == f"sqlite+aiosqlite:///{expected_dir / 'aiteam.db'}"


class TestMigrateOldDb:
    """Verify the old-database automatic migration logic."""

    def test_migrate_old_db(self, tmp_path: Path) -> None:
        """When the old DB exists and the new DB is empty, migrate automatically."""
        old_db = tmp_path / "old" / "aiteam.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_bytes(b"x" * 20000)  # >10KB

        new_db = tmp_path / "new" / "aiteam.db"
        new_db.parent.mkdir(parents=True)

        with patch("aiteam.storage.connection.Path.cwd", return_value=old_db.parent):
            _migrate_old_db_if_needed(new_db)

        assert new_db.exists()
        assert new_db.stat().st_size == 20000
        # The old file should be renamed
        assert not old_db.exists()
        assert old_db.with_suffix(".db.migrated").exists()

    def test_skip_migrate_when_new_db_has_data(self, tmp_path: Path) -> None:
        """Do not migrate when the new DB already has data."""
        old_db = tmp_path / "old" / "aiteam.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_bytes(b"old" * 10000)

        new_db = tmp_path / "new" / "aiteam.db"
        new_db.parent.mkdir(parents=True)
        new_db.write_bytes(b"new" * 10000)  # >10KB

        with patch("aiteam.storage.connection.Path.cwd", return_value=old_db.parent):
            _migrate_old_db_if_needed(new_db)

        # The new DB content is unchanged
        assert new_db.read_bytes() == b"new" * 10000
        # The old DB was not renamed
        assert old_db.exists()

    def test_migrate_silent_on_error(self, tmp_path: Path) -> None:
        """Errors should be handled silently without raising."""
        new_db = tmp_path / "nonexistent" / "deep" / "aiteam.db"
        # The parent directory of new_db does not exist, so stat will fail, but it should not raise
        with patch("aiteam.storage.connection.Path.cwd", return_value=tmp_path):
            _migrate_old_db_if_needed(new_db)  # Should not raise
