"""AI Team OS CLI — standalone company commands (no Claude Code required)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aiteam.cli.display import print_error
from aiteam.cli.manager import run_async

app = typer.Typer(name="standalone", help="Standalone company (no Claude Code)")
console = Console()


@app.command("org")
def show_org() -> None:
    """Show the standalone company org chart."""
    from aiteam.standalone import COMPANY_ROLES, COMPANY_TEAM_NAME

    table = Table(title=f"Standalone company — team '{COMPANY_TEAM_NAME}'")
    table.add_column("Role")
    table.add_column("Held by")
    table.add_row("Chairman", "You (human owner) — mission & approvals")
    table.add_row("CEO", "Leader (plan → delegate → synthesize)")
    for name, role, _prompt in COMPANY_ROLES:
        table.add_row(role, f"agent '{name}'")
    console.print(table)
    console.print(
        "Execution: direct LLM API (anthropic/openai) or offline fake — "
        "no Claude Code, no ~/.claude, no hooks."
    )


@app.command("run")
def run_standalone(
    task: str = typer.Option(..., "--task", help="Task for the company to execute"),
    provider: str = typer.Option(
        None,
        "--provider",
        help="LLM provider: anthropic | openai | fake (default: AITEAM_LLM_PROVIDER or anthropic)",
    ),
    model: str = typer.Option(None, "--model", help="Model name (provider default if omitted)"),
) -> None:
    """Run a task through the standalone company (CEO + COO + CTO + team)."""
    try:
        from aiteam.standalone import run_company_task

        console.print("[bold blue]Running standalone company task...[/bold blue]")
        result = run_async(
            run_company_task(task, provider=provider, model=model)
        )

        panel = Panel(
            f"[bold]Task ID:[/bold] {result.task_id}\n"
            f"[bold]Status:[/bold] {result.status.value}\n"
            f"[bold]Duration:[/bold] {result.duration_seconds:.1f}s\n\n"
            f"[bold]Result:[/bold]\n{result.result}",
            title="[bold green]Standalone company — task result[/bold green]",
            border_style="green",
        )
        console.print(panel)

        if result.agent_outputs:
            for agent_name, output in result.agent_outputs.items():
                console.print(
                    Panel(output, title=f"[bold cyan]{agent_name}[/bold cyan]", border_style="cyan")
                )

    except Exception as e:
        print_error(f"Standalone run failed: {e}")
        raise typer.Exit(code=1) from None
