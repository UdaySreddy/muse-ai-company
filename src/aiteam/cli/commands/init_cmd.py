"""AI Team OS CLI — init command."""

from __future__ import annotations

from pathlib import Path

import typer

from aiteam.cli.display import print_error, print_success, print_warning
from aiteam.config.settings import AITEAM_DIR, CONFIG_FILENAME, generate_default_config

app = typer.Typer(name="init", help="Initialize a project")


# ============================================================
# Template configuration
# ============================================================

TEMPLATES: dict[str, str] = {
    "research": """\
# AI Team OS project configuration — research team template
project:
  name: "research-project"
  description: "AI research project"
  language: "zh"

defaults:
  model: "opus"  # tier-level alias, automatically follows the latest version
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "research-team"
  mode: "coordinate"
  leader:
    name: "lead-researcher"
    role: "Chief Researcher"
    system_prompt: "You are a senior researcher responsible for formulating """
"""research plans and synthesizing analysis results."
  members:
    - name: "literature-analyst"
      role: "Literature Analyst"
      system_prompt: "You are responsible for retrieving and analyzing relevant literature."
    - name: "data-analyst"
      role: "Data Analyst"
      system_prompt: "You are responsible for data processing, statistical analysis, and visualization."
""",
    "development": """\
# AI Team OS project configuration — development team template
project:
  name: "dev-project"
  description: "Software development project"
  language: "zh"

defaults:
  model: "opus"  # tier-level alias, automatically follows the latest version
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "dev-team"
  mode: "coordinate"
  leader:
    name: "tech-lead"
    role: "Technical Director"
    system_prompt: "You are the technical director, responsible for task """
"""decomposition, code review, and technical decisions."
  members:
    - name: "backend-dev"
      role: "Backend Developer"
      system_prompt: "You are responsible for the design and implementation of backend services."
    - name: "frontend-dev"
      role: "Frontend Developer"
      system_prompt: "You are responsible for the design and implementation of the frontend interface."
    - name: "qa-engineer"
      role: "Test Engineer"
      system_prompt: "You are responsible for writing test cases and ensuring code quality."
""",
    "analysis": """\
# AI Team OS project configuration — analysis team template
project:
  name: "analysis-project"
  description: "Data analysis project"
  language: "zh"

defaults:
  model: "opus"  # tier-level alias, automatically follows the latest version
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"
  memory_backend: "file"
  dashboard_port: 3000
  api_port: 8000

team:
  name: "analysis-team"
  mode: "coordinate"
  leader:
    name: "lead-analyst"
    role: "Chief Analyst"
    system_prompt: "You are the chief analyst, responsible for formulating analysis plans and summarizing conclusions."
  members:
    - name: "data-collector"
      role: "Data Collector"
      system_prompt: "You are responsible for data collection and preprocessing."
    - name: "data-modeler"
      role: "Data Modeler"
      system_prompt: "You are responsible for building analysis models and in-depth analysis."
""",
}


# ============================================================
# Command implementation
# ============================================================


@app.callback(invoke_without_command=True)
def init(
    template: str | None = typer.Option(  # noqa: UP007
        None,
        "--template",
        "-t",
        help="Project template (research/development/analysis)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Force-overwrite the existing configuration"),
) -> None:
    """Initialize project and generate aiteam.yaml config file."""
    project_dir = Path.cwd()
    config_path = project_dir / CONFIG_FILENAME
    aiteam_dir = project_dir / AITEAM_DIR

    # Check if config already exists
    if config_path.exists() and not force:
        overwrite = typer.confirm(f"{CONFIG_FILENAME} already exists; overwrite it?")
        if not overwrite:
            print_warning("Initialization cancelled")
            raise typer.Exit()

    # Determine config content
    if template:
        if template not in TEMPLATES:
            print_error(f"Unknown template '{template}'; available templates: {', '.join(TEMPLATES.keys())}")
            raise typer.Exit(code=1)
        config_content = TEMPLATES[template]
    else:
        config_content = generate_default_config()

    # Write config file
    config_path.write_text(config_content, encoding="utf-8")
    print_success(f"Generated {CONFIG_FILENAME}")

    # Create the .aiteam directory
    aiteam_dir.mkdir(exist_ok=True)
    print_success(f"Created the {AITEAM_DIR}/ directory")

    if template:
        print_success(f"Initialized the project using the '{template}' template")
    else:
        print_success("Project initialization complete; please edit aiteam.yaml to configure your team")
