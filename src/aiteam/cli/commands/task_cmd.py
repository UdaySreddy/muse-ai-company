"""AI Team OS CLI — task command group."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from aiteam.cli.display import print_error, print_tasks_table
from aiteam.cli.manager import get_manager, run_async

app = typer.Typer(name="task", help="Task management")
console = Console()


@app.command("run")
def run_task(
    team: str = typer.Option(..., "--team", "-t", help="Team name"),
    task: str = typer.Option(..., "--task", help="Task description"),
) -> None:
    """Execute a task."""
    try:
        manager = get_manager()
        console.print(f"[bold blue]Running task...[/bold blue] Team: {team}")
        result = run_async(manager.run_task(team_name=team, task_description=task))

        # Display result
        panel = Panel(
            f"[bold]Task ID:[/bold] {result.task_id}\n"
            f"[bold]Status:[/bold] {result.status.value}\n"
            f"[bold]Elapsed:[/bold] {result.duration_seconds:.1f}s\n\n"
            f"[bold]Result:[/bold]\n{result.result}",
            title="[bold green]Task execution result[/bold green]",
            border_style="green",
        )
        console.print(panel)

        # Display each Agent's output
        if result.agent_outputs:
            for agent_name, output in result.agent_outputs.items():
                agent_panel = Panel(
                    output,
                    title=f"[bold cyan]{agent_name}[/bold cyan]",
                    border_style="cyan",
                )
                console.print(agent_panel)

    except Exception as e:
        print_error(f"Failed to execute task: {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
def list_tasks(
    team: str = typer.Option(..., "--team", "-t", help="Team name"),
) -> None:
    """List tasks for a team."""
    try:
        manager = get_manager()
        tasks = run_async(manager.list_tasks(team))
        print_tasks_table(tasks)
    except Exception as e:
        print_error(f"Failed to list tasks: {e}")
        raise typer.Exit(code=1) from None


@app.command("status")
def task_status(
    task_id: str = typer.Argument(help="Task ID"),
) -> None:
    """View task details."""
    try:
        manager = get_manager()
        task = run_async(manager.get_task_status(task_id))

        status_color = {
            "pending": "yellow",
            "running": "blue",
            "completed": "green",
            "failed": "red",
        }.get(task.status.value, "white")

        content = (
            f"[bold]Task ID:[/bold] {task.id}\n"
            f"[bold]Title:[/bold] {task.title}\n"
            f"[bold]Description:[/bold] {task.description or '-'}\n"
            f"[bold]Status:[/bold] [{status_color}]{task.status.value}[/{status_color}]\n"
            f"[bold]Assigned to:[/bold] {task.assigned_to or '-'}\n"
            f"[bold]Created:[/bold] {task.created_at:%Y-%m-%d %H:%M:%S}"
        )
        if task.started_at:
            content += f"\n[bold]Started:[/bold] {task.started_at:%Y-%m-%d %H:%M:%S}"
        if task.completed_at:
            content += f"\n[bold]Completed:[/bold] {task.completed_at:%Y-%m-%d %H:%M:%S}"
        if task.result:
            content += f"\n\n[bold]Result:[/bold]\n{task.result}"

        panel = Panel(content, title="[bold blue]Task details[/bold blue]", border_style="blue")
        console.print(panel)

    except Exception as e:
        print_error(f"Failed to get task details: {e}")
        raise typer.Exit(code=1) from None
