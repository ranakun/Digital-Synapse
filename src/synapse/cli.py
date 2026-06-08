"""Digital Synapse command line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated

import typer

from synapse import __version__
from synapse.config import init_vault
from synapse.embeddings import embed_entities
from synapse.importers import import_linkedin_connections
from synapse.index import reindex
from synapse.ingest import commit_proposed, ingest_file, proposed_entities
from synapse.maintenance import check_vault, merge_entities
from synapse.nlquery import execute_question
from synapse.queries import filter_entities, find_entities, neighbors, path_between
from synapse.web import export_html
from synapse.web import serve as serve_web

app = typer.Typer(help="Digital Synapse personal knowledge graph.", invoke_without_command=True)

VaultOption = Annotated[Path, typer.Option("--vault", help="Vault root path.")]


def echo_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the Digital Synapse version and exit.",
    ),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("init")
def init_command(
    path: Annotated[Path, typer.Option("--path", "-p", help="Vault path to initialize.")] = Path(
        "."
    ),
    no_git: Annotated[bool, typer.Option("--no-git", help="Do not initialize git.")] = False,
) -> None:
    root = init_vault(path, initialize_git=not no_git)
    echo_json({"vault": str(root), "initialized": True})


@app.command("reindex")
def reindex_command(
    vault: VaultOption = Path("."),
    full: Annotated[
        bool, typer.Option("--full", help="Drop and rebuild the derived index.")
    ] = False,
) -> None:
    result = reindex(vault, full=full)
    echo_json(
        {
            "entities": result.entities,
            "relations": result.relations,
            "changed_files": result.changed_files,
            "issues": [
                {
                    "severity": issue.severity,
                    "message": issue.message,
                    "file_path": str(issue.file_path) if issue.file_path else None,
                }
                for issue in result.issues
            ],
        }
    )
    if result.has_errors:
        raise typer.Exit(2)


@app.command("find")
def find_command(
    text: Annotated[str, typer.Argument(help="Text to search.")],
    vault: VaultOption = Path("."),
) -> None:
    echo_json(find_entities(vault, text))


@app.command("neighbors")
def neighbors_command(
    entity_id: Annotated[str, typer.Argument(help="Start entity id.")],
    vault: VaultOption = Path("."),
    depth: Annotated[int, typer.Option("--depth", "-d", min=1)] = 1,
    rel: Annotated[list[str] | None, typer.Option("--rel", help="Relation type filter.")] = None,
    undirected: Annotated[
        bool, typer.Option("--undirected", help="Traverse edges both ways.")
    ] = False,
    include_weak: Annotated[
        bool, typer.Option("--include-weak", help="Include weak wikilink edges.")
    ] = False,
) -> None:
    echo_json(
        neighbors(
            vault,
            entity_id,
            depth=depth,
            relation_types=rel,
            undirected=undirected,
            include_weak=include_weak,
        ).to_dict()
    )


@app.command("path")
def path_command(
    id_a: Annotated[str, typer.Argument(help="Start entity id.")],
    id_b: Annotated[str, typer.Argument(help="End entity id.")],
    vault: VaultOption = Path("."),
    max_hops: Annotated[int, typer.Option("--max-hops", min=1)] = 4,
    all_matches: Annotated[bool, typer.Option("--all", help="Return all shortest paths.")] = False,
    include_weak: Annotated[
        bool, typer.Option("--include-weak", help="Include weak wikilink edges.")
    ] = False,
) -> None:
    echo_json(
        path_between(
            vault, id_a, id_b, max_hops=max_hops, all_paths=all_matches, include_weak=include_weak
        ).to_dict()
    )


@app.command("filter")
def filter_command(
    vault: VaultOption = Path("."),
    entity_type: Annotated[str | None, typer.Option("--type", help="Entity type.")] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Tag.")] = None,
    property_key: Annotated[str | None, typer.Option("--property", help="Property key.")] = None,
    property_value: Annotated[str | None, typer.Option("--value", help="Property value.")] = None,
) -> None:
    echo_json(
        filter_entities(
            vault,
            entity_type=entity_type,
            tag=tag,
            property_key=property_key,
            property_value=property_value,
        )
    )


@app.command("ingest")
def ingest_command(
    path: Annotated[Path, typer.Argument(help="Source file to ingest.")],
    vault: VaultOption = Path("."),
    no_watch: Annotated[
        bool, typer.Option("--no-watch", help="Accepted for SDS CLI compatibility.")
    ] = False,
) -> None:
    _ = no_watch
    try:
        echo_json(ingest_file(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc


@app.command("import-linkedin-connections")
def import_linkedin_connections_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Connections.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_connections(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("status")
def status_command(vault: VaultOption = Path(".")) -> None:
    echo_json({"proposed": proposed_entities(vault)})


@app.command("commit")
def commit_command(
    vault: VaultOption = Path("."),
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Git commit message.")
    ] = None,
) -> None:
    result = commit_proposed(vault, message)
    echo_json(result)
    if not result["committed"]:
        raise typer.Exit(1)


@app.command("query")
def query_command(
    question: Annotated[
        str, typer.Argument(help="Natural-language question or deterministic shorthand.")
    ],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(execute_question(question, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("serve")
def serve_command(
    vault: VaultOption = Path("."),
    port: Annotated[int, typer.Option("--port", "-p", help="Local port.")] = 7777,
) -> None:
    typer.echo(f"Serving Digital Synapse on http://127.0.0.1:{port}")
    serve_web(vault, port=port)


@app.command("export")
def export_command(
    query_text: Annotated[str, typer.Argument(help="Query or deterministic shorthand.")],
    html: Annotated[Path, typer.Option("--html", help="Output HTML file.")],
    vault: VaultOption = Path("."),
) -> None:
    path = export_html(vault, query_text, html)
    echo_json({"html": str(path)})


@app.command("check")
def check_command(vault: VaultOption = Path(".")) -> None:
    echo_json(check_vault(vault))


@app.command("merge")
def merge_command(
    id_a: Annotated[str, typer.Argument(help="Entity id to keep.")],
    id_b: Annotated[str, typer.Argument(help="Entity id to merge/archive.")],
    vault: VaultOption = Path("."),
) -> None:
    echo_json(merge_entities(id_a, id_b, vault=vault))


@app.command("embed")
def embed_command(
    vault: VaultOption = Path("."),
    all_entities_flag: Annotated[
        bool, typer.Option("--all", help="Refresh all embeddings.")
    ] = False,
) -> None:
    _ = all_entities_flag
    echo_json(embed_entities(vault))


@app.command("watch")
def watch_command(
    vault: VaultOption = Path("."),
    interval: Annotated[float, typer.Option("--interval", help="Polling interval seconds.")] = 2.0,
) -> None:
    from synapse.config import load_config

    cfg = load_config(vault)
    inbox = Path(vault) / cfg["ingestion"]["inbox_dir"]
    seen = {path for path in inbox.glob("*") if path.is_file()}
    typer.echo(f"Watching {inbox}")
    while True:
        current = {path for path in inbox.glob("*") if path.is_file()}
        for path in sorted(current - seen):
            typer.echo(f"Ingesting {path}")
            ingest_file(path, vault=vault)
        seen = current
        time.sleep(interval)
