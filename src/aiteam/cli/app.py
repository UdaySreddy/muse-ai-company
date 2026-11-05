"""AI Team OS CLI — main entry point."""

import typer
from rich.console import Console

from aiteam import __version__
from aiteam.cli.commands import (
    agent_cmd,
    init_cmd,
    standalone_cmd,
    status_cmd,
    task_cmd,
    team_cmd,
    up_cmd,
)

app = typer.Typer(
    name="aiteam",
    help="AI Team OS — a general-purpose, reusable AI agent team operating system",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold blue]AI Team OS[/bold blue] v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", help="Show the version number", callback=version_callback, is_eager=True
    ),
) -> None:
    """AI Team OS — a general-purpose, reusable AI agent team operating system."""


# Register subcommand groups
app.add_typer(init_cmd.app, name="init", help="Initialize a project and generate the aiteam.yaml config file")
app.add_typer(team_cmd.app, name="team", help="Team management")
app.add_typer(agent_cmd.app, name="agent", help="Agent management")
app.add_typer(task_cmd.app, name="task", help="Task management")
app.add_typer(status_cmd.app, name="status", help="View team and task status")
app.add_typer(standalone_cmd.app, name="standalone", help="Standalone company (no Claude Code)")
app.add_typer(up_cmd.app, name="up", help="Start the API server")
# `aiteam hooks` was retired (2026-07-27, batch 8.5): it wrote into the project-level .claude/settings.local.json
# only the single send_event script with the old 7-event matcher; alongside the global chain (install.py → ~/.claude/
# settings.json, 11 events / 17 entries) every event fired twice. The single registration entry point = the repo-root
# install.py (since batch 5, the I8 machine check pins install.py ↔ hooks.json ↔ README in lockstep).


if __name__ == "__main__":
    app()
