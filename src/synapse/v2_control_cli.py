"""Explicit owner-host commands; never installed as worker tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from synapse.codex_events import CodexEvents, CodexSessionHost
from synapse.codex_host import CodexReasoner
from synapse.host_control import HostControl, read_json
from synapse.host_session import NativeHost
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error

app = typer.Typer(help="Explicit trusted owner-host operations.")
Vault = Annotated[Path, typer.Option("--vault")]


def output(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def fail(exc):
    output({"error": exc.to_dict() if isinstance(exc, V2Error) else {"code": "invalid-request", "message": str(exc)}})
    raise typer.Exit(1) from exc


@app.command("events")
def events(thread: Annotated[str | None, typer.Option("--thread")] = None, limit: Annotated[int, typer.Option(min=1, max=50)] = 12):
    """Find actual owner/display events in the current local Codex task."""
    try:
        output({"events": CodexEvents.for_thread(thread).recent(limit=limit), "captured": False})
    except V2Error as exc:
        fail(exc)


@app.command("native")
def native(operation: str, arguments_json: Annotated[str, typer.Option("--arguments-json")] = "{}", input_file: Annotated[Path | None, typer.Option("--input")] = None, owner_event: Annotated[str | None, typer.Option("--owner-event")] = None, thread: Annotated[str | None, typer.Option("--thread")] = None, model: Annotated[str | None, typer.Option("--model")] = None, vault: Vault = Path(".")):
    """Parent-host bridge using actual Codex owner/display messages.

    start delegates a bounded request; execute-run uses Codex CLI, while a
    native worker may reason using read tools then supply admit/stage inputs.
    Ordinary questions use v2 ask or the read-only tools instead.
    """
    try:
        arguments = read_json(input_file) if input_file else json.loads(arguments_json)
        if not isinstance(arguments, dict):
            raise V2Error("invalid-request", "Arguments must be a JSON object")
        task_id = thread or os.environ.get("CODEX_THREAD_ID", "")
        host = CodexSessionHost(vault, CodexEvents.for_thread(task_id), reasoner=CodexReasoner(model=model), thread_id=task_id)
        result = HostControl(host).execute(operation, arguments, owner_event_ref=owner_event, progress=lambda value: typer.echo(f"Synapse: {value['phase']}", err=True))
        output(result)
    except KeyboardInterrupt:
        output({"status": "cancelled", "message": "Execution stopped. Retained findings and receipts remain available; use status before explicitly continuing."})
        raise typer.Exit(130) from None
    except (V2Error, ValueError, KeyError, TypeError) as exc:
        fail(exc)


@app.command("terminal")
def terminal(operation: str, input_file: Annotated[Path, typer.Option("--input")], model: Annotated[str | None, typer.Option("--model")] = None, vault: Vault = Path(".")):
    """Use a direct terminal owner input when a native host is unavailable.

    For review, displays the ready brief and accepts one actual reply. For
    other operations the input is the owner's instruction, never an approval
    boolean embedded in a worker packet.
    """
    events = {}
    host = NativeHost(vault, event_reader=events.__getitem__, display=typer.echo, reasoner=CodexReasoner(model=model), host_id="terminal")
    try:
        arguments = read_json(input_file)
        if operation == "review":
            shown = host.show_proposal(arguments["proposal_id"], arguments["version"])
            text = typer.prompt("Your decision")
            event_id = generate_ulid()
            events[event_id] = {"id": event_id, "actor": "user", "text": text}
            output(host.reply(event_id, display_id=shown["display_id"]))
            return
        event_id = generate_ulid()
        events[event_id] = {"id": event_id, "actor": "user", "text": typer.prompt("Your instruction")}
        output(HostControl(host).execute(operation, arguments, owner_event_ref=event_id))
    except (V2Error, ValueError, KeyError, TypeError) as exc:
        fail(exc)


@app.command("migration-preview")
def migration_preview(vault: Vault = Path(".")):
    """Read and fingerprint the exact v1 baseline proposed for activation."""
    from synapse.v2_migration import prepare
    try:
        snapshot = prepare(vault)
        output({"snapshot_hash": snapshot.review_fingerprint, "report": snapshot.report, "activated": False})
    except V2Error as exc:
        fail(exc)


@app.command("migration-trial")
def migration_trial(destination: Path, vault: Vault = Path(".")):
    """Copy the vault and rehearse activation there; no live cutover."""
    from synapse.v2_migration import trial
    try:
        output(trial(vault, destination))
    except V2Error as exc:
        fail(exc)


@app.command("prepare-import")
def prepare_import(source: Path, importer: Annotated[str, typer.Option("--importer")], output_file: Annotated[Path, typer.Option("--output")], options_json: Annotated[str, typer.Option("--options-json")] = "{}", vault: Vault = Path(".")):
    """Write inert importer candidates for a delegated proposal builder."""
    from synapse.import_candidates import prepare_import as prepare
    try:
        if source.name == ".env" or output_file.name == ".env":
            raise V2Error("invalid-path", "Environment files cannot be importer inputs or outputs")
        candidate = prepare(vault, source, importer=importer, options=json.loads(options_json))
        changes = [{**change, "raw": change["raw"].decode("utf-8")} for change in candidate["changes"]]
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("x", encoding="utf-8") as handle:
            json.dump({"base_revision": candidate["base_revision"], "source": str(source.resolve()), "changes": changes, "summary": candidate["summary"], "limitations": candidate["limitations"]}, handle, ensure_ascii=False, indent=2, default=str)
        output({"candidate_file": str(output_file.resolve()), "changes": len(changes), "published": False, "next": "Explicitly capture the original source, then build/stage a faithful brief in a delegated run."})
    except (V2Error, ValueError, OSError) as exc:
        fail(exc)
