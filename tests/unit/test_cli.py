"""AI Team OS — CLI command unit tests."""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from aiteam import __version__
from aiteam.cli.app import app
from aiteam.config.settings import CONFIG_FILENAME

runner = CliRunner()


class TestVersion:
    """Test the version number output."""

    def test_version_flag(self) -> None:
        """`aiteam --version` outputs the version number (following the real package version; hard-coding is not
        allowed)."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output
        assert __version__ in result.output

    def test_version_short_flag(self) -> None:
        """`aiteam -v` likewise outputs the version number."""
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output


class TestHelp:
    """Test help information."""

    def test_main_help(self) -> None:
        """`aiteam --help` displays help information."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "AI Team OS" in result.output

    def test_no_args_shows_help(self) -> None:
        """`aiteam` with no arguments displays help (Typer returns exit code 2)."""
        result = runner.invoke(app, [])
        # Typer's no_args_is_help returns exit code 2
        assert result.exit_code in (0, 2)
        assert "AI Team OS" in result.output


class TestInitCommand:
    """Test the init command."""

    def test_init_creates_yaml(self, tmp_path: Path) -> None:
        """`aiteam init` generates an aiteam.yaml file."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        assert config_file.exists()
        content = config_file.read_text(encoding="utf-8")
        assert "my-project" in content
        assert "sqlite" in content

    def test_init_creates_aiteam_dir(self, tmp_path: Path) -> None:
        """`aiteam init` creates a .aiteam/ directory."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / ".aiteam").is_dir()

    def test_init_with_template_research(self, tmp_path: Path) -> None:
        """`aiteam init --template research` includes the research team configuration."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "research"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "research-team" in content
        assert "lead-researcher" in content
        assert "literature-analyst" in content
        assert "data-analyst" in content

    def test_init_with_template_development(self, tmp_path: Path) -> None:
        """`aiteam init --template development` includes the development team configuration."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "development"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "dev-team" in content
        assert "tech-lead" in content
        assert "backend-dev" in content

    def test_init_with_template_analysis(self, tmp_path: Path) -> None:
        """`aiteam init --template analysis` includes the analysis team configuration."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "analysis"])
        assert result.exit_code == 0
        config_file = tmp_path / CONFIG_FILENAME
        content = config_file.read_text(encoding="utf-8")
        assert "analysis-team" in content
        assert "lead-analyst" in content

    def test_init_invalid_template(self, tmp_path: Path) -> None:
        """`aiteam init --template invalid` errors and exits."""
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--template", "invalid"])
        assert result.exit_code != 0

    def test_init_force_overwrite(self, tmp_path: Path) -> None:
        """`aiteam init --force` forcibly overwrites an existing configuration."""
        os.chdir(tmp_path)
        # Create a configuration file first
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("old content", encoding="utf-8")
        # Force the overwrite
        result = runner.invoke(app, ["init", "--force"])
        assert result.exit_code == 0
        content = config_file.read_text(encoding="utf-8")
        assert "my-project" in content
        assert "old content" not in content

    def test_init_existing_config_cancel(self, tmp_path: Path) -> None:
        """`aiteam init` with an existing configuration, the user cancels."""
        os.chdir(tmp_path)
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("old content", encoding="utf-8")
        # Enter "n" to cancel the overwrite
        result = runner.invoke(app, ["init"], input="n\n")
        assert result.exit_code == 0
        # The content is unchanged
        content = config_file.read_text(encoding="utf-8")
        assert content == "old content"


class TestSubcommandHelp:
    """Test subcommand help information."""

    def test_team_help(self) -> None:
        result = runner.invoke(app, ["team", "--help"])
        assert result.exit_code == 0

    def test_agent_help(self) -> None:
        result = runner.invoke(app, ["agent", "--help"])
        assert result.exit_code == 0

    def test_task_help(self) -> None:
        result = runner.invoke(app, ["task", "--help"])
        assert result.exit_code == 0

    def test_status_help(self) -> None:
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
