"""Standalone v2 read and direct-consultation CLI.

The root CLI imports ``app`` later; keeping this module standalone makes read
commands available without changing legacy command registration here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from synapse.codex_host import CodexReasoner
from synapse.gateway import Gateway
from synapse.specialist import Specialist, request_for
from synapse.v2_contracts import V2Error, validate_payload
from synapse.v2_control_cli import app as owner_app
from synapse.v2_protocol import dispatch

app = typer.Typer(help="Digital Synapse v2 read protocol and direct consultation.")
app.add_typer(owner_app, name="owner")
VaultOption = Annotated[Path, typer.Option("--vault", help="Vault root path.")]


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True), nl=False)


def _read_arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("must be a JSON object") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("must be a JSON object")
    return value


def _dispatch_or_exit(
    vault: Path,
    operation: str,
    arguments: dict[str, Any],
    *,
    revision: str | None,
    known_at: str | None,
    timezone: str,
    budget_chars: int,
) -> None:
    value = dispatch(
        vault,
        operation,
        arguments,
        revision=revision,
        known_at=known_at,
        timezone=timezone,
        budget_chars=budget_chars,
    )
    _print(value)
    if "error" in value:
        raise typer.Exit(1)


@app.command("describe")
def describe(
    vault: VaultOption = Path("."),
    budget_chars: Annotated[int, typer.Option("--budget-chars", min=256, max=32000)] = 8000,
) -> None:
    """Describe the read protocol and current retained coverage."""

    _dispatch_or_exit(
        vault,
        "describe",
        {},
        revision=None,
        known_at=None,
        timezone=None,
        budget_chars=budget_chars,
    )


@app.command("read")
def read(
    operation: Annotated[str, typer.Argument(help="Allowlisted read operation.")],
    arguments_json: Annotated[
        str, typer.Option("--arguments-json", help="JSON object of operation arguments.")
    ] = "{}",
    revision: Annotated[str | None, typer.Option("--revision")] = None,
    known_at: Annotated[str | None, typer.Option("--known-at")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    budget_chars: Annotated[int, typer.Option("--budget-chars", min=256, max=32000)] = 8000,
    vault: VaultOption = Path("."),
) -> None:
    """Dispatch one explicitly allowlisted read."""

    _dispatch_or_exit(
        vault,
        operation,
        _read_arguments(arguments_json),
        revision=revision,
        known_at=known_at,
        timezone=timezone,
        budget_chars=budget_chars,
    )


@app.command("context")
def context(
    query: Annotated[str | None, typer.Argument(help="Optional lexical query.")] = None,
    ids: Annotated[
        list[str] | None, typer.Option("--id", help="Exact record ID; repeat as needed.")
    ] = None,
    subject: Annotated[str | None, typer.Option("--subject")] = None,
    facet: Annotated[str | None, typer.Option("--facet")] = None,
    knowledge_policy: Annotated[str, typer.Option("--knowledge-policy")] = "mixed",
    revision: Annotated[str | None, typer.Option("--revision")] = None,
    known_at: Annotated[str | None, typer.Option("--known-at")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    budget_chars: Annotated[int, typer.Option("--budget-chars", min=256, max=32000)] = 8000,
    vault: VaultOption = Path("."),
) -> None:
    """Read correction-aware context by query or exact IDs."""

    arguments: dict[str, Any] = {"knowledge_policy": knowledge_policy}
    if query is not None:
        arguments["query"] = query
    if ids:
        arguments["ids"] = ids
    if subject is not None:
        arguments["subject_id"] = subject
    if facet is not None:
        arguments["facet"] = facet
    _dispatch_or_exit(
        vault,
        "context",
        arguments,
        revision=revision,
        known_at=known_at,
        timezone=timezone,
        budget_chars=budget_chars,
    )


@app.command("source")
def source(
    id: Annotated[str, typer.Argument(help="Source ID.")],
    version: Annotated[str | None, typer.Option("--version")] = None,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 4000,
    revision: Annotated[str | None, typer.Option("--revision")] = None,
    known_at: Annotated[str | None, typer.Option("--known-at")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    budget_chars: Annotated[int, typer.Option("--budget-chars", min=256, max=32000)] = 8000,
    vault: VaultOption = Path("."),
) -> None:
    """Read a retained source version in Unicode-character pages."""

    arguments: dict[str, Any] = {"id": id, "offset": offset, "limit": limit}
    if version is not None:
        arguments["version"] = version
    _dispatch_or_exit(
        vault,
        "source",
        arguments,
        revision=revision,
        known_at=known_at,
        timezone=timezone,
        budget_chars=budget_chars,
    )


@app.command("record")
def record(
    id: Annotated[str, typer.Argument(help="Record ID.")],
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 4000,
    revision: Annotated[str | None, typer.Option("--revision")] = None,
    known_at: Annotated[str | None, typer.Option("--known-at")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    budget_chars: Annotated[int, typer.Option("--budget-chars", min=256, max=32000)] = 8000,
    vault: VaultOption = Path("."),
) -> None:
    """Read retained Markdown in Unicode-character pages."""

    _dispatch_or_exit(
        vault,
        "record",
        {"id": id, "offset": offset, "limit": limit},
        revision=revision,
        known_at=known_at,
        timezone=timezone,
        budget_chars=budget_chars,
    )


def _cancelled_result(vault: Path, request: dict[str, Any]) -> dict[str, Any]:
    gateway = Gateway(vault)
    result = {
        "request_id": request["id"],
        "status": "cancelled",
        "stop_reason": "Owner cancelled the consultation.",
        "knowledge_revision": gateway.revision,
        "answer": "The consultation was cancelled before a result was available.",
        "records": [],
        "evidence": [],
        "provisional_dependencies": [],
        "alternatives": [],
        "uncertainties": ["No conclusion is supported because the consultation was cancelled."],
        "coverage": {
            "catalogued": len(gateway.view.manifest["records"]),
            "metadata_inspected": 0,
            "source_passages_read": 0,
            "returned_records": 0,
            "omitted_records": 0,
            "limitations": ["Consultation cancelled before retrieval completed."],
            "index_state": "current",
            "semantic_search": "unavailable",
        },
        "proposal_ids": [],
        "suggestion_ids": [],
        "receipt_ids": [],
    }
    validate_payload("result", result)
    return result


@app.command("ask")
def ask(
    purpose: Annotated[str, typer.Argument(help="Question or intent for the consultation.")],
    context_text: Annotated[str, typer.Option("--context")] = "No additional context supplied.",
    subject: Annotated[
        list[str] | None, typer.Option("--subject", help="Subject ID; repeat as needed.")
    ] = None,
    preset: Annotated[str, typer.Option("--preset", help="consult, focused or broad.")] = "consult",
    model: Annotated[
        str | None, typer.Option("--model", help="Optional explicit Codex model.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    semantic: Annotated[bool, typer.Option("--semantic", help="Explicitly warm the already installed local semantic model for this consultation.")] = False,
    vault: VaultOption = Path("."),
) -> None:
    """Run one explicitly requested direct consultation through Codex."""

    request = request_for(purpose, context=context_text, subject_ids=subject or [], preset=preset)
    try:
        reasoner = CodexReasoner(model=model)
        from synapse.v2_runtime import warm_semantics
        with warm_semantics(vault, enabled=semantic):
            result = Specialist(vault, reasoner).run(request)
    except KeyboardInterrupt:
        result = _cancelled_result(vault, request)
    except V2Error as exc:
        _print({"error": exc.to_dict()})
        raise typer.Exit(1) from exc
    if json_output:
        _print(result)
        return
    typer.echo(result["answer"])
    uncertainties = result.get("uncertainties", [])
    if uncertainties:
        typer.echo("Material uncertainties:")
        for item in uncertainties:
            typer.echo(f"- {item}")
    suggestions = result.get("suggestion_ids", [])
    if suggestions:
        typer.echo("Materially used suggestions: " + ", ".join(suggestions))


__all__ = ["app"]


@app.command("prepare-semantic")
def prepare_semantic(vault: VaultOption = Path("."), download: Annotated[bool, typer.Option("--download", help="Allow downloading the configured local embedding model.")] = False):
    """Explicitly build disposable vectors for the current retained revision."""
    from synapse.config import load_config
    from synapse.embeddings import FastEmbedder
    from synapse.v2_semantic import build_index
    try:
        model = load_config(vault)["embeddings"]["model"]
        embedder = FastEmbedder(model, threads=2, local_files_only=not download)
        result = build_index(vault, embedder=embedder)
        _print(result)
        if result["semantic"] != "ok":
            raise typer.Exit(1)
    except (V2Error, RuntimeError, OSError) as exc:
        _print({"error": exc.to_dict() if isinstance(exc, V2Error) else {"code": "unsupported-operation", "message": str(exc)}})
        raise typer.Exit(1) from exc
