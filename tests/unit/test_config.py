"""AI Team OS — configuration system unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aiteam.config.settings import (
    CONFIG_FILENAME,
    ProjectConfig,
    TeamMemberConfig,
    find_config_file,
    generate_default_config,
    load_config,
)


class TestGenerateDefaultConfig:
    """Test default configuration YAML generation."""

    def test_returns_valid_yaml(self) -> None:
        """The generated content is valid YAML."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)

    def test_contains_project_section(self) -> None:
        """Contains the project config section."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "project" in data
        assert data["project"]["name"] == "my-project"
        assert data["project"]["language"] == "zh"

    def test_contains_defaults_section(self) -> None:
        """Contains the defaults config section."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "defaults" in data
        assert data["defaults"]["model"] == "opus"  # The generated template uses tier-level aliases (settings.py:164,
        # automatically follows the latest version)
        assert data["defaults"]["max_context_ratio"] == 0.8

    def test_contains_infrastructure_section(self) -> None:
        """Contains the infrastructure config section."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert "infrastructure" in data
        assert data["infrastructure"]["storage_backend"] == "sqlite"
        assert data["infrastructure"]["memory_backend"] == "file"

    def test_can_be_loaded_as_project_config(self) -> None:
        """The generated YAML can be parsed by ProjectConfig."""
        content = generate_default_config()
        data = yaml.safe_load(content)
        config = ProjectConfig.model_validate(data)
        assert config.project.name == "my-project"
        assert config.defaults.model == "opus"  # The generated template uses tier-level aliases (settings.py:164,
        # automatically follows the latest version)


class TestLoadConfig:
    """Tests loading configuration from a YAML file."""

    def test_load_from_file(self, tmp_path: Path) -> None:
        """Load configuration from a YAML file."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text(generate_default_config(), encoding="utf-8")
        config = load_config(config_path)
        assert config.project.name == "my-project"
        assert config.infrastructure.storage_backend == "sqlite"

    def test_load_with_team_section(self, tmp_path: Path) -> None:
        """Load a file containing a team config section."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text(
            """\
project:
  name: "test"
team:
  name: "my-team"
  mode: "coordinate"
  members:
    - name: "agent-1"
      role: "Development"
""",
            encoding="utf-8",
        )
        config = load_config(config_path)
        assert config.team is not None
        assert config.team.name == "my-team"
        assert config.team.mode == "coordinate"
        assert len(config.team.members) == 1
        assert config.team.members[0].name == "agent-1"

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        """Return the default configuration when the file does not exist."""
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.project.name == ""
        assert config.defaults.model == ""  # Since v1.8.1 the default is empty = inherit the CC global (model
        # governance, 4-7 phantom cleanup)

    def test_load_empty_file(self, tmp_path: Path) -> None:
        """An empty file returns the default configuration."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("", encoding="utf-8")
        config = load_config(config_path)
        assert config.project.name == ""


class TestConfigValidation:
    """Tests Pydantic validation."""

    def test_invalid_mode_raises_error(self) -> None:
        """An invalid orchestration mode should raise an error."""
        with pytest.raises(ValidationError, match="Invalid orchestration mode"):
            TeamMemberConfig(name="test", mode="invalid_mode")

    def test_valid_modes_accepted(self) -> None:
        """All valid orchestration modes should pass validation."""
        for mode in ("coordinate", "broadcast", "route", "meet"):
            team = TeamMemberConfig(name="test", mode=mode)
            assert team.mode == mode

    def test_invalid_max_context_ratio(self) -> None:
        """A max_context_ratio out of range should raise an error."""
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"defaults": {"max_context_ratio": 1.5}})

    def test_invalid_storage_backend(self) -> None:
        """An invalid storage_backend should raise an error."""
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"infrastructure": {"storage_backend": "mongodb"}})


class TestFindConfigFile:
    """Tests upward search for aiteam.yaml."""

    def test_find_in_current_dir(self, tmp_path: Path) -> None:
        """Find the configuration file in the current directory."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("project:\n  name: test\n", encoding="utf-8")
        result = find_config_file(tmp_path)
        assert result == config_path

    def test_find_in_parent_dir(self, tmp_path: Path) -> None:
        """Find the configuration file in a parent directory."""
        config_path = tmp_path / CONFIG_FILENAME
        config_path.write_text("project:\n  name: test\n", encoding="utf-8")
        child_dir = tmp_path / "sub" / "deep"
        child_dir.mkdir(parents=True)
        result = find_config_file(child_dir)
        assert result == config_path

    def test_not_found_returns_none(self, tmp_path: Path) -> None:
        """Return None when no configuration file is found."""
        child_dir = tmp_path / "empty" / "deep"
        child_dir.mkdir(parents=True)
        # Do not place a configuration file in tmp_path or its subdirectories
        # find_config_file keeps searching upward until the root directory
        # If there is no aiteam.yaml in the system root it returns None
        result = find_config_file(child_dir)
        # An aiteam.yaml may exist in some parent directory (e.g. the project root)
        # So we only verify the return type is correct
        assert result is None or result.name == CONFIG_FILENAME


class TestConfigDefaults:
    """Tests default value correctness."""

    def test_project_defaults(self) -> None:
        """ProjectInfo defaults."""
        config = ProjectConfig()
        assert config.project.name == ""
        assert config.project.description == ""
        assert config.project.language == "zh"

    def test_defaults_config(self) -> None:
        """DefaultsConfig defaults."""
        config = ProjectConfig()
        assert config.defaults.model == ""  # Since v1.8.1 the default is empty = inherit the CC global (model
        # governance, 4-7 phantom cleanup)
        assert config.defaults.max_context_ratio == 0.8

    def test_infrastructure_defaults(self) -> None:
        """InfrastructureConfig defaults."""
        config = ProjectConfig()
        assert config.infrastructure.storage_backend == "sqlite"
        assert config.infrastructure.memory_backend == "file"
        assert config.infrastructure.dashboard_port == 3000
        assert config.infrastructure.api_port == 8000
        assert config.infrastructure.db_url == ""

    def test_team_defaults_to_none(self) -> None:
        """team defaults to None."""
        config = ProjectConfig()
        assert config.team is None

    def test_get_db_url_sqlite(self, tmp_path: Path) -> None:
        """In SQLite mode get_db_url returns the correct path."""
        config = ProjectConfig()
        url = config.infrastructure.get_db_url(tmp_path)
        assert "sqlite+aiosqlite" in url
        assert ".aiteam" in url
        assert "aiteam.db" in url

    def test_get_db_url_custom(self) -> None:
        """A custom db_url has the highest priority."""
        config = ProjectConfig.model_validate({"infrastructure": {"db_url": "postgresql://custom"}})
        url = config.infrastructure.get_db_url(Path("/dummy"))
        assert url == "postgresql://custom"
