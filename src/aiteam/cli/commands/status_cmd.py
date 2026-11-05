"""AI Team OS CLI — global status command."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from aiteam.cli.display import print_error, print_status, print_warning
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="status", help="Status overview")
console = Console()


@app.callback(invoke_without_command=True)
def status(
    team: str | None = typer.Option(None, "--team", "-t", help="Specify a team name (default: show the global "
        "overview)"),
) -> None:
    """View global status overview."""
    try:
        manager = get_manager()

        if team:
            # Show specified team status
            team_status = run_async(manager.get_status(team))
            print_status(team_status)
        else:
            # Global overview: list all teams and their summaries
            teams = run_async(manager.list_teams())

            if not teams:
                print_warning(
                    "No teams yet. Use 'aiteam team create' to create a team, or 'aiteam init' to initialize a project."
                )
                return

            # Summary table
            table = Table(title="Global status overview")
            table.add_column("Team", style="bold cyan")
            table.add_column("Orchestration mode", style="green")
            table.add_column("Agents", justify="right")
            table.add_column("Active tasks", justify="right", style="yellow")
            table.add_column("Completed", justify="right", style="green")
            table.add_column("Total tasks", justify="right")

            for t in teams:
                try:
                    s = run_async(manager.get_status(t.name))
                    table.add_row(
                        t.name,
                        t.mode.value,
                        str(len(s.agents)),
                        str(len(s.active_tasks)),
                        str(s.completed_tasks),
                        str(s.total_tasks),
                    )
                except Exception:
                    table.add_row(
                        t.name,
                        t.mode.value,
                        "-",
                        "-",
                        "-",
                        "-",
                    )

            console.print(table)

    except Exception as e:
        print_error(f"Failed to get status: {e}")
        raise typer.Exit(code=1) from None
