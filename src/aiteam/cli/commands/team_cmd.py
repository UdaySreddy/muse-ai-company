"""AI Team OS CLI — team management command group."""

from __future__ import annotations

import typer

from aiteam.cli.display import print_error, print_success, print_team, print_teams_table
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="team", help="Team management")


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", "-n", help="Team name"),
    mode: str = typer.Option(
        "coordinate", "--mode", "-m", help="Orchestration mode (coordinate/broadcast/route/meet)"
    ),
) -> None:
    """Create a new team."""
    try:
        manager = get_manager()
        team = run_async(manager.create_team(name=name, mode=mode))
        print_success(f"Team '{team.name}' created successfully")
        print_team(team)
    except Exception as e:
        print_error(f"Failed to create team: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_teams() -> None:
    """List all teams."""
    try:
        manager = get_manager()
        teams = run_async(manager.list_teams())
        print_teams_table(teams)
    except Exception as e:
        print_error(f"Failed to list teams: {e}")
        raise typer.Exit(code=1) from None


@app.command("show")
def show(
    name: str = typer.Argument(help="Team name or ID"),
) -> None:
    """View team details."""
    try:
        manager = get_manager()
        team = run_async(manager.get_team(name))
        print_team(team)
    except Exception as e:
        print_error(f"Failed to get team details: {e}")
        raise typer.Exit(code=1) from None


@app.command("delete")
def delete(
    name: str = typer.Argument(help="Team name or ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Delete a team."""
    if not yes:
        confirmed = typer.confirm(f"Are you sure you want to delete team '{name}'? This action cannot be undone")
        if not confirmed:
            print_success("Deletion cancelled")
            raise typer.Exit()

    try:
        manager = get_manager()
        result = run_async(manager.delete_team(name))
        if result:
            print_success(f"Team '{name}' deleted")
        else:
            print_error(f"Team '{name}' not found")
    except Exception as e:
        print_error(f"Failed to delete team: {e}")
        raise typer.Exit(code=1) from None


@app.command("set-mode")
def set_mode(
    name: str = typer.Argument(help="Team name or ID"),
    mode: str = typer.Option(
        ..., "--mode", "-m", help="Orchestration mode (coordinate/broadcast/route/meet)"
    ),
) -> None:
    """Set team orchestration mode."""
    try:
        manager = get_manager()
        team = run_async(manager.set_mode(name, mode))
        print_success(f"Team '{team.name}' orchestration mode set to '{mode}'")
    except Exception as e:
        print_error(f"Failed to set orchestration mode: {e}")
        raise typer.Exit(code=1) from None
