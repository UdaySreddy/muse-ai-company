"""AI Team OS CLI — Agent management command group."""

from __future__ import annotations

import typer

from aiteam.cli.display import print_agents_table, print_error, print_success
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="agent", help="Agent management")


@app.command("create")
def create(
    team: str = typer.Option(..., "--team", "-t", help="Team name"),
    name: str = typer.Option(..., "--name", "-n", help="Agent name"),
    role: str = typer.Option(..., "--role", "-r", help="Agent role"),
    model: str | None = typer.Option(None, "--model", "-m", help="Model to use"),  # noqa: UP007
) -> None:
    """Create a new Agent."""
    try:
        manager = get_manager()
        kwargs: dict[str, str] = {}
        if model:
            kwargs["model"] = model
        agent = run_async(manager.add_agent(team_name=team, name=name, role=role, **kwargs))
        print_success(f"Agent '{agent.name}' added to team '{team}'")
    except Exception as e:
        print_error(f"Failed to create agent: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_agents(
    team: str = typer.Option(..., "--team", "-t", help="Team name"),
) -> None:
    """List all Agents in a team."""
    try:
        manager = get_manager()
        agents = run_async(manager.list_agents(team))
        print_agents_table(agents)
    except Exception as e:
        print_error(f"Failed to list agents: {e}")
        raise typer.Exit(code=1) from None


@app.command("remove")
def remove(
    team: str = typer.Option(..., "--team", "-t", help="Team name"),
    name: str = typer.Option(..., "--name", "-n", help="Agent name"),
) -> None:
    """Remove an Agent from a team."""
    try:
        manager = get_manager()
        result = run_async(manager.remove_agent(team_name=team, agent_name=name))
        if result:
            print_success(f"Agent '{name}' removed from team '{team}'")
        else:
            print_error(f"Agent '{name}' not found in team '{team}'")
    except Exception as e:
        print_error(f"Failed to remove agent: {e}")
        raise typer.Exit(code=1) from None
