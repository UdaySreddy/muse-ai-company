"""AI Team OS CLI — Rich formatted output utilities."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aiteam.clock import ensure_utc
from aiteam.types import Agent, Task, Team, TeamStatusSummary

console = Console()


def _local(value: datetime | None) -> str:
    """Render a stored (UTC) timestamp in the operator's own timezone.

    Storage and transport are UTC everywhere (see aiteam/clock.py); presentation
    is the one place that should speak the reader's clock — a terminal table
    showing 09:00 for something that happened at 17:00 helps nobody.
    """
    aware = ensure_utc(value)
    return aware.astimezone().strftime("%Y-%m-%d %H:%M") if aware else "-"


# ============================================================
# Status messages
# ============================================================


def print_success(msg: str) -> None:
    """Print success message."""
    console.print(f"[bold green][OK][/bold green] {msg}")


def print_error(msg: str) -> None:
    """Print error message."""
    console.print(f"[bold red][ERROR][/bold red] {msg}")


def print_warning(msg: str) -> None:
    """Print warning message."""
    console.print(f"[bold yellow][WARN][/bold yellow] {msg}")


# ============================================================
# Team display
# ============================================================


def print_team(team: Team) -> None:
    """Display team details using Rich Panel."""
    content = (
        f"[bold]Name:[/bold] {team.name}\n"
        f"[bold]Orchestration mode:[/bold] {team.mode.value}\n"
        f"[bold]ID:[/bold] {team.id}\n"
        f"[bold]Created:[/bold] {team.created_at:%Y-%m-%d %H:%M:%S}\n"
        f"[bold]Updated:[/bold] {team.updated_at:%Y-%m-%d %H:%M:%S}"
    )
    if team.config:
        content += f"\n[bold]Config:[/bold] {team.config}"

    panel = Panel(content, title=f"[bold blue]Team: {team.name}[/bold blue]", border_style="blue")
    console.print(panel)


def print_teams_table(teams: list[Team]) -> None:
    """List teams using Rich Table."""
    if not teams:
        print_warning("No teams yet")
        return

    table = Table(title="Team list")
    table.add_column("Name", style="bold cyan")
    table.add_column("Orchestration mode", style="green")
    table.add_column("ID", style="dim")
    table.add_column("Created")

    for team in teams:
        table.add_row(
            team.name,
            team.mode.value,
            team.id[:8] + "...",
            _local(team.created_at),
        )

    console.print(table)


# ============================================================
# Agent display
# ============================================================


def print_agents_table(agents: list[Agent]) -> None:
    """List agents using Rich Table."""
    if not agents:
        print_warning("No agents yet")
        return

    table = Table(title="Agent list")
    table.add_column("Name", style="bold cyan")
    table.add_column("Role", style="green")
    table.add_column("Model", style="yellow")
    table.add_column("Status", style="magenta")
    table.add_column("ID", style="dim")

    for agent in agents:
        status_color = {
            "busy": "green",
            "waiting": "blue",
            "offline": "dim",
        }.get(agent.status.value, "white")

        table.add_row(
            agent.name,
            agent.role,
            agent.model,
            f"[{status_color}]{agent.status.value}[/{status_color}]",
            agent.id[:8] + "...",
        )

    console.print(table)


# ============================================================
# Task display
# ============================================================


def print_tasks_table(tasks: list[Task]) -> None:
    """List tasks using Rich Table."""
    if not tasks:
        print_warning("No tasks yet")
        return

    table = Table(title="Task list")
    table.add_column("ID", style="dim")
    table.add_column("Title", style="bold cyan")
    table.add_column("Status", style="magenta")
    table.add_column("Assigned to")
    table.add_column("Created")

    for task in tasks:
        status_color = {
            "pending": "yellow",
            "running": "blue",
            "completed": "green",
            "failed": "red",
        }.get(task.status.value, "white")

        table.add_row(
            task.id[:8] + "...",
            task.title,
            f"[{status_color}]{task.status.value}[/{status_color}]",
            task.assigned_to or "-",
            _local(task.created_at),
        )

    console.print(table)


# ============================================================
# Status panel
# ============================================================


def print_status(status: TeamStatusSummary) -> None:
    """Comprehensive status panel."""
    # Team info
    team = status.team
    content = (
        f"[bold]Team name:[/bold] {team.name}\n"
        f"[bold]Orchestration mode:[/bold] {team.mode.value}\n"
        f"[bold]Agent count:[/bold] {len(status.agents)}\n"
        f"[bold]Active tasks:[/bold] {len(status.active_tasks)}\n"
        f"[bold]Completed tasks:[/bold] {status.completed_tasks}\n"
        f"[bold]Total tasks:[/bold] {status.total_tasks}"
    )

    panel = Panel(
        content,
        title=f"[bold blue]Status: {team.name}[/bold blue]",
        border_style="blue",
    )
    console.print(panel)

    # Agent list
    if status.agents:
        print_agents_table(status.agents)

    # Active tasks
    if status.active_tasks:
        print_tasks_table(status.active_tasks)
