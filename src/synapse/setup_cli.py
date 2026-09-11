"""Agent-executable setup and everyday lifecycle commands."""

from pathlib import Path
from typing import Annotated

import typer

from synapse import setup
from synapse.lifecycle import LifecycleError
from synapse.v2_contracts import V2Error

app = typer.Typer(help="Guided Mac/Codex setup. Agents execute these steps for the user.")
DEFAULT_HOME = setup.default_home()
Home = Annotated[Path, typer.Option("--home", help="Explicit Synapse installation directory.")]


def emit(function, *args, **kwargs):
    import json

    try:
        typer.echo(json.dumps(function(*args, **kwargs), indent=2, ensure_ascii=False, default=str))
    except (V2Error, LifecycleError, OSError, ValueError) as exc:
        error = (
            exc.to_dict()
            if isinstance(exc, V2Error)
            else {"code": "setup-failed", "message": str(exc)}
        )
        typer.echo(json.dumps({"error": error}))
        raise typer.Exit(1) from exc


@app.command("initialize")
def initialize(home: Home = DEFAULT_HOME, timezone: str = "UTC", purpose: str = ""):
    emit(setup.initialize, home, timezone=timezone, purpose=purpose)


@app.command("status")
def status(home: Home = DEFAULT_HOME):
    emit(setup.status, home)


@app.command("prepare")
def prepare(
    home: Home = DEFAULT_HOME,
    semantic: Annotated[bool | None, typer.Option("--semantic/--no-semantic")] = None,
):
    emit(setup.prepare, home, semantic=semantic)


@app.command("connect-codex")
def connect(home: Home = DEFAULT_HOME):
    emit(setup.connect_codex, home)


@app.command("open")
def open_viewer(home: Home = DEFAULT_HOME, browser: bool = True):
    import webbrowser

    from synapse.lifecycle import start_viewer

    def run():
        result = start_viewer(home, Path(setup.settings(home)["vault"]))
        if browser and result.get("url"):
            webbrowser.open(result["url"])
        return result

    emit(run)


@app.command("stop")
def stop(home: Home = DEFAULT_HOME):
    from synapse.lifecycle import stop_viewer

    emit(stop_viewer, home)


@app.command("backup")
def backup(destination: Path, home: Home = DEFAULT_HOME):
    from synapse.lifecycle import backup_workspace

    emit(lambda: backup_workspace(Path(setup.settings(home)["vault"]), destination))


@app.command("restore")
def restore(backup: Path, destination: Path):
    from synapse.lifecycle import restore_workspace

    emit(restore_workspace, backup, destination)


@app.command("mcp")
def mcp(home: Home = DEFAULT_HOME):
    # stdout is exclusively MCP protocol traffic.
    setup.run_mcp(home)
