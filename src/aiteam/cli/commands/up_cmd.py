"""AI Team OS CLI — up command.

Start the API server (uvicorn).
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer()
console = Console()


@app.callback(invoke_without_command=True)
def up(
    api_port: int = typer.Option(8000, "--api-port", help="API server port"),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="Start the API only, without the Dashboard"),
    reload: bool = typer.Option(False, "--reload", help="Development mode with auto-reload"),
) -> None:
    """Start the AI Team OS API server."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error: uvicorn is not installed; please run pip install 'uvicorn[standard]'[/red]")
        raise typer.Exit(1)

    console.print("[bold blue]AI Team OS[/bold blue] API server starting...")
    console.print(f"  API address: [green]http://localhost:{api_port}[/green]")
    console.print(f"  API docs: [green]http://localhost:{api_port}/docs[/green]")

    if no_dashboard:
        console.print("  Dashboard: [dim]disabled[/dim]")
    else:
        console.print("  Dashboard: [yellow]not yet integrated (use --no-dashboard to skip)[/yellow]")

    console.print()

    uvicorn.run(
        "aiteam.api.app:create_app",
        host="0.0.0.0",
        port=api_port,
        reload=reload,
        factory=True,
    )
