"""Digital Synapse command line interface."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from synapse import __version__
from synapse.brief import build_entity_brief, build_owner_brief
from synapse.career import generate_career_report
from synapse.config import init_vault
from synapse.dossier import DEFAULT_DOSSIER_BUDGET_TOKENS, build_dossier
from synapse.embeddings import embed_entities
from synapse.enrichment import (
    attach_linkedin_profile,
    generate_enrichment_queue,
    import_linkedin_profile_pdf,
)
from synapse.guide import write_agent_guide
from synapse.importers import (
    import_linkedin_certifications,
    import_linkedin_connections,
    import_linkedin_education,
    import_linkedin_endorsements_given,
    import_linkedin_endorsements_received,
    import_linkedin_events,
    import_linkedin_invitations,
    import_linkedin_job_applications,
    import_linkedin_messages,
    import_linkedin_positions,
    import_linkedin_recommendations_given,
    import_linkedin_recommendations_received,
    import_linkedin_saved_jobs,
    import_linkedin_skills,
)
from synapse.index import connect, reindex, resolve_entity_ref
from synapse.ingest import commit_proposed, proposed_entities
from synapse.maintenance import (
    AmbiguousRefError,
    check_vault,
    merge_entities,
    verify_entities,
    verify_report,
)
from synapse.nlquery import execute_question
from synapse.owner_context import build_owner_context, format_knowledge_notice
from synapse.queries import filter_entities, find_entities, neighbors, path_between
from synapse.setup_cli import app as setup_app
from synapse.util import encode_for_console
from synapse.v2_cli import app as v2_app
from synapse.warmpath import rank_connectors
from synapse.web import export_html
from synapse.web import serve as serve_web
from synapse.whatsapp import import_whatsapp_chat

app = typer.Typer(help="Digital Synapse personal knowledge graph.", invoke_without_command=True)

app.add_typer(v2_app, name="v2")

app.add_typer(setup_app, name="setup")

VaultOption = Annotated[Path, typer.Option("--vault", help="Vault root path.")]


def _configure_utf8_console() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def echo_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def safe_echo(message: str = "", *, err: bool = False) -> None:
    """typer.echo that never raises UnicodeEncodeError on legacy consoles.

    Use for any output containing vault-derived text (names, bodies, briefs).
    """
    stream = sys.stderr if err else sys.stdout
    typer.echo(encode_for_console(message, getattr(stream, "encoding", None)), err=err)


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the Digital Synapse version and exit.",
    ),
) -> None:
    _configure_utf8_console()
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("init")
def init_command(
    path: Annotated[Path, typer.Option("--path", "-p", help="Vault path to initialize.")] = Path(
        "."
    ),
    owner_name: Annotated[
        str, typer.Option("--owner-name", help="Name for the vault owner entity.")
    ] = "Me",
    no_git: Annotated[bool, typer.Option("--no-git", help="Do not initialize git.")] = False,
) -> None:
    root = init_vault(path, initialize_git=not no_git, owner_name=owner_name)
    try:
        write_agent_guide(root)
    except Exception:
        pass
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
    no_reindex: Annotated[
        bool, typer.Option("--no-reindex", help="skip change detection; use when you just reindexed.")
    ] = False,
) -> None:
    start_time = time.perf_counter()
    fallback_tracker = []
    results = find_entities(vault, text, reindex=not no_reindex, fallback_tracker=fallback_tracker)
    duration_ms = int((time.perf_counter() - start_time) * 1000)
    result_count = len(results)
    zero_hit = (result_count == 0)
    fallback = "substring" if "substring" in fallback_tracker else None
    
    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "find",
        "params": {"text": text},
        "result_count": result_count,
        "duration_ms": duration_ms,
        "zero_hit": zero_hit,
        "fallback": fallback,
    })
    echo_json(results)


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
    no_reindex: Annotated[
        bool, typer.Option("--no-reindex", help="skip change detection; use when you just reindexed.")
    ] = False,
) -> None:
    start_time = time.perf_counter()
    res = neighbors(
        vault,
        entity_id,
        depth=depth,
        relation_types=rel,
        undirected=undirected,
        include_weak=include_weak,
        reindex=not no_reindex,
    )
    duration_ms = int((time.perf_counter() - start_time) * 1000)
    result_count = len(res.edges)
    zero_hit = (result_count == 0)
    
    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "neighbors",
        "params": {
            "start": entity_id,
            "depth": depth,
            "rel": rel,
            "undirected": undirected,
            "include_weak": include_weak,
        },
        "result_count": result_count,
        "duration_ms": duration_ms,
        "zero_hit": zero_hit,
        "fallback": None,
    })
    for edge in res.edges:
        from_name = edge.get("from_name") or edge["from_id"]
        to_name = edge.get("to_name") or edge["to_id"]
        line = f"{from_name} ({edge['from_id']}) —{edge['type']}→ {to_name} ({edge['to_id']})"
        try:
            typer.echo(line)
        except UnicodeEncodeError:
            safe_echo(f"{from_name} ({edge['from_id']}) -{edge['type']}-> {to_name} ({edge['to_id']})")


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
    no_reindex: Annotated[
        bool, typer.Option("--no-reindex", help="skip change detection; use when you just reindexed.")
    ] = False,
) -> None:
    start_time = time.perf_counter()
    res = path_between(
        vault,
        id_a,
        id_b,
        max_hops=max_hops,
        all_paths=all_matches,
        include_weak=include_weak,
        reindex=not no_reindex,
    )
    duration_ms = int((time.perf_counter() - start_time) * 1000)
    result_count = len(res.edges)
    zero_hit = (result_count == 0)
    
    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "path",
        "params": {
            "start": id_a,
            "end": id_b,
            "max_hops": max_hops,
            "all": all_matches,
            "include_weak": include_weak,
        },
        "result_count": result_count,
        "duration_ms": duration_ms,
        "zero_hit": zero_hit,
        "fallback": None,
    })
    for edge in res.edges:
        from_name = edge.get("from_name") or edge["from_id"]
        to_name = edge.get("to_name") or edge["to_id"]
        line = f"{from_name} ({edge['from_id']}) —{edge['type']}→ {to_name} ({edge['to_id']})"
        try:
            typer.echo(line)
        except UnicodeEncodeError:
            safe_echo(f"{from_name} ({edge['from_id']}) -{edge['type']}-> {to_name} ({edge['to_id']})")


@app.command("filter")
def filter_command(
    vault: VaultOption = Path("."),
    entity_type: Annotated[str | None, typer.Option("--type", help="Entity type.")] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Tag.")] = None,
    property_key: Annotated[str | None, typer.Option("--property", help="Property key.")] = None,
    property_value: Annotated[str | None, typer.Option("--value", help="Property value.")] = None,
    no_reindex: Annotated[
        bool, typer.Option("--no-reindex", help="skip change detection; use when you just reindexed.")
    ] = False,
) -> None:
    start_time = time.perf_counter()
    results = filter_entities(
        vault,
        entity_type=entity_type,
        tag=tag,
        property_key=property_key,
        property_value=property_value,
        reindex=not no_reindex,
    )
    duration_ms = int((time.perf_counter() - start_time) * 1000)
    result_count = len(results)
    zero_hit = (result_count == 0)
    
    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "filter",
        "params": {
            "type": entity_type,
            "tag": tag,
            "property": property_key,
            "value": property_value,
        },
        "result_count": result_count,
        "duration_ms": duration_ms,
        "zero_hit": zero_hit,
        "fallback": None,
    })
    echo_json(results)


@app.command("ingest")
def ingest_command(
    path: Annotated[Path, typer.Argument(help="Legacy free-form source path.")],
    vault: VaultOption = Path("."),
    no_watch: Annotated[
        bool, typer.Option("--no-watch", help="Accepted for SDS CLI compatibility.")
    ] = False,
) -> None:
    _ = path, vault, no_watch
    typer.echo(
        "Free-form ingest has no API-key runtime. Ask Codex to inspect the source and create "
        "a reviewable proposal, or use a deterministic import-* command.",
        err=True,
    )
    raise typer.Exit(2)


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


@app.command("import-linkedin-certifications")
def import_linkedin_certifications_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Certifications.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_certifications(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-positions")
def import_linkedin_positions_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Positions.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_positions(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-education")
def import_linkedin_education_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Education.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_education(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-skills")
def import_linkedin_skills_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Skills.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_skills(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-recommendations-received")
def import_linkedin_recommendations_received_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Recommendations Received file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_recommendations_received(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-recommendations-given")
def import_linkedin_recommendations_given_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Recommendations Given file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_recommendations_given(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-saved-jobs")
def import_linkedin_saved_jobs_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Saved Jobs.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_saved_jobs(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-job-applications")
def import_linkedin_job_applications_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Job Applications.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_job_applications(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-invitations")
def import_linkedin_invitations_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Invitations.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_invitations(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-messages")
def import_linkedin_messages_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn messages.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_messages(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-endorsements-received")
def import_linkedin_endorsements_received_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Endorsement_Received_Info.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_endorsements_received(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-endorsements-given")
def import_linkedin_endorsements_given_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Endorsement_Given_Info.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_endorsements_given(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-events")
def import_linkedin_events_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Events.csv file.")],
    vault: VaultOption = Path("."),
) -> None:
    try:
        echo_json(import_linkedin_events(path, vault=vault))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("enrichment-queue")
def enrichment_queue_command(
    vault: VaultOption = Path("."),
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
    keyword: Annotated[
        list[str] | None,
        typer.Option("--keyword", help="Override ranking keywords; repeat as needed."),
    ] = None,
    geography: Annotated[
        list[str] | None,
        typer.Option("--geography", help="Override preferred geographies; repeat as needed."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Queue path relative to the vault."),
    ] = None,
) -> None:
    try:
        echo_json(
            generate_enrichment_queue(
                vault,
                limit=limit,
                keywords=keyword,
                geographies=geography,
                output=output,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("enrichment-attach")
def enrichment_attach_command(
    path: Annotated[Path, typer.Argument(help="Downloaded LinkedIn profile PDF.")],
    person: Annotated[str, typer.Option("--person", help="Existing person entity ID.")],
    captured_at: Annotated[
        str, typer.Option("--captured-at", help="Profile capture date (YYYY-MM-DD).")
    ],
    vault: VaultOption = Path("."),
    queue: Annotated[
        Path | None,
        typer.Option("--queue", help="Queue path relative to the vault."),
    ] = None,
) -> None:
    try:
        echo_json(
            attach_linkedin_profile(
                path,
                person_id=person,
                captured_at=captured_at,
                vault=vault,
                queue=queue,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-linkedin-profile")
def import_linkedin_profile_command(
    path: Annotated[Path, typer.Argument(help="LinkedIn Save-to-PDF profile file.")],
    person: Annotated[str, typer.Option("--person", help="Existing person entity ID.")],
    captured_at: Annotated[
        str, typer.Option("--captured-at", help="Profile capture date (YYYY-MM-DD).")
    ],
    vault: VaultOption = Path("."),
    queue: Annotated[
        Path | None,
        typer.Option("--queue", help="Queue path relative to the vault."),
    ] = None,
) -> None:
    try:
        echo_json(
            import_linkedin_profile_pdf(
                path,
                person_id=person,
                captured_at=captured_at,
                vault=vault,
                queue=queue,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("import-whatsapp-chat")
def import_whatsapp_chat_command(
    path: Annotated[Path, typer.Argument(help="WhatsApp exported chat text file.")],
    chat_key: Annotated[
        str,
        typer.Option("--chat-key", help="Stable explicit key for this chat across exports."),
    ],
    vault: VaultOption = Path("."),
    since: Annotated[
        str | None,
        typer.Option("--since", help="Import messages on or after YYYY-MM-DD."),
    ] = None,
    min_messages: Annotated[
        int,
        typer.Option("--min-messages", min=1, help="Minimum messages after filters."),
    ] = 2,
    date_order: Annotated[
        str,
        typer.Option("--date-order", help="auto, day-first, or month-first."),
    ] = "auto",
    chat_label: Annotated[
        str | None,
        typer.Option("--chat-label", help="Reviewed display label for the chat."),
    ] = None,
    participant: Annotated[
        list[str] | None,
        typer.Option(
            "--participant",
            help="Explicit sender binding as LABEL=ENTITY_ID; repeat as needed.",
        ),
    ] = None,
) -> None:
    mappings: dict[str, str] = {}
    for item in participant or []:
        if "=" not in item:
            typer.echo(f"Invalid --participant value {item!r}; expected LABEL=ENTITY_ID", err=True)
            raise typer.Exit(1)
        label, entity_id = item.split("=", 1)
        if not label.strip() or not entity_id.strip():
            typer.echo(f"Invalid --participant value {item!r}; expected LABEL=ENTITY_ID", err=True)
            raise typer.Exit(1)
        mappings[label.strip()] = entity_id.strip()
    try:
        echo_json(
            import_whatsapp_chat(
                path,
                chat_key=chat_key,
                vault=vault,
                since=since,
                min_messages=min_messages,
                date_order=date_order,
                chat_label=chat_label,
                participant_mappings=mappings,
            )
        )
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
        str,
        typer.Argument(
            help="Deterministic shorthand; use Codex + MCP for free-form questions."
        ),
    ],
    vault: VaultOption = Path("."),
) -> None:
    try:
        start_time = time.perf_counter()
        results = execute_question(question, vault=vault)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        
        nodes_count = len(results.get("nodes", []))
        edges_count = len(results.get("edges", []))
        zero_hit = (nodes_count == 0) or (edges_count == 0 and nodes_count <= 1)
        result_count = edges_count if edges_count > 0 else nodes_count
        
        from synapse.querylog import append as log_append
        log_append(vault, {
            "iface": "cli",
            "op": "query",
            "params": {"question": question},
            "result_count": result_count,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None,
        })
        echo_json(results)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("serve")
def serve_command(
    vault: VaultOption = Path("."),
    port: Annotated[int, typer.Option("--port", "-p", help="Local port.")] = 7777,
    semantic: Annotated[bool, typer.Option("--semantic", help="Use the already installed local semantic model and prepared vectors for v2 reads.")] = False,
) -> None:
    typer.echo(f"Serving Digital Synapse on http://127.0.0.1:{port}")
    if semantic:
        from synapse.revisions import is_v2
        from synapse.v2_runtime import warm_semantics
        if not is_v2(Path(vault)):
            raise typer.BadParameter("--semantic on the viewer requires an activated v2 vault")
        with warm_semantics(vault):
            serve_web(vault, port=port)
    else:
        serve_web(vault, port=port)


@app.command("mcp")
def mcp_command(
    vault: VaultOption = Path("."),
    semantic: Annotated[bool, typer.Option("--semantic", help="Warm the installed local semantic model for retained v2 reads.")] = False,
) -> None:
    """Run the read-only Model Context Protocol (MCP) server over stdio."""
    try:
        from synapse.mcpserver import run_mcp_server
    except ImportError as exc:
        typer.echo(
            "Model Context Protocol (MCP) server dependencies are not installed.\n"
            "Please run: pip install -e \".[mcp]\"",
            err=True,
        )
        raise typer.Exit(1) from exc

    _run_mcp_with_semantics(vault, run_mcp_server, semantic=semantic)


def _run_mcp_with_semantics(vault, runner, *, semantic=False, **kwargs):
    if not semantic:
        return runner(vault, **kwargs)
    from synapse.revisions import is_v2
    from synapse.v2_runtime import warm_semantics
    if not is_v2(Path(vault)):
        raise typer.BadParameter("--semantic requires an activated v2 vault")
    with warm_semantics(vault):
        return runner(vault, **kwargs)


@app.command("mcp-http")
def mcp_http_command(
    vault: VaultOption = Path("."),
    semantic: Annotated[bool, typer.Option("--semantic", help="Warm the installed local semantic model for retained v2 reads.")] = False,
    host: Annotated[
        str,
        typer.Option("--host", help="Loopback address for the shared service."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Local Streamable HTTP port."),
    ] = 8765,
) -> None:
    """Run the shared, read-only MCP service over Streamable HTTP."""
    try:
        from synapse.mcpserver import run_mcp_http_server
    except ImportError as exc:
        typer.echo(
            "Model Context Protocol (MCP) server dependencies are not installed.\n"
            "Please run: pip install -e \".[mcp]\"",
            err=True,
        )
        raise typer.Exit(1) from exc

    typer.echo(f"Serving Digital Synapse MCP on http://{host}:{port}/mcp")
    _run_mcp_with_semantics(vault, run_mcp_http_server, semantic=semantic, host=host, port=port)


@app.command("eval")
def eval_command(
    questions: Annotated[Path | None, typer.Option("--questions", help="Path to YAML questions file.")] = None,
    vault: VaultOption = Path("."),
    json_out: Annotated[bool, typer.Option("--json", help="Output results in JSON format.")] = False,
) -> None:
    """Run golden-question evaluations against the vault."""
    from synapse.config import resolve_vault
    from synapse.evals import append_history, load_questions, run_all
    
    try:
        root = resolve_vault(vault)
    except Exception as exc:
        typer.echo(f"Error resolving vault: {exc}", err=True)
        raise typer.Exit(2) from exc
        
    q_path = questions if questions is not None else root / "evals" / "questions.yaml"
    if not q_path.exists():
        typer.echo("no questions authored yet — see docs/design/09", err=True)
        raise typer.Exit(2)
        
    try:
        q_list = load_questions(q_path)
    except Exception as exc:
        typer.echo(f"Error loading questions: {exc}", err=True)
        raise typer.Exit(2) from exc
        
    try:
        results, summary = run_all(root, q_list)
        append_history(root, summary)
    except Exception as exc:
        typer.echo(f"Error running evaluations: {exc}", err=True)
        raise typer.Exit(2) from exc
        
    if json_out:
        res_data = []
        for r in results:
            res_data.append({
                "id": r.question_id,
                "question": r.question,
                "passed": r.passed,
                "error": r.error,
                "warnings": r.warnings,
                "details": r.details,
            })
        echo_json({
            "results": res_data,
            "summary": summary,
        })
    else:
        typer.echo("Evaluation Results")
        typer.echo("==================")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            safe_echo(f"{r.question_id:<20} | {status}")
            for w in r.warnings:
                safe_echo(f"  Warning: {w}")
            if r.error:
                safe_echo(f"  Error: {r.error}")
            elif r.details:
                safe_echo(f"  Failure: {r.details}")
        typer.echo("")
        typer.echo(f"Summary: {summary['pass']} passed, {summary['fail']} failed ({len(results)} total)")
        
    if summary["fail"] > 0:
        raise typer.Exit(1)


@app.command("eval-blind")
def eval_blind_command(
    questions: Annotated[
        Path | None,
        typer.Option("--questions", help="Candidate YAML; only question text reaches the model."),
    ] = None,
    vault: VaultOption = Path("."),
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Run fresh subscription-authenticated Codex tasks after isolation preflight.",
        ),
    ] = False,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    question_id: Annotated[str | None, typer.Option("--question-id")] = None,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 2,
) -> None:
    """Preflight or run a capability-isolated Codex evaluation."""
    from synapse.blind_eval import assert_isolated_payload, run_isolated_suite
    from synapse.config import resolve_vault
    from synapse.evals import load_questions

    root = resolve_vault(vault)
    path = questions or root / "evals" / "proposed-questions.yaml"
    try:
        candidates = load_questions(path)
        selected = [item for item in candidates if not question_id or item["id"] == question_id]
        if question_id and not selected:
            raise ValueError(f"Unknown question id: {question_id}")
        if limit is not None:
            selected = selected[:limit]
        for item in selected:
            assert_isolated_payload(item)
    except Exception as exc:
        typer.echo(f"Isolation preflight failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(
        f"Isolation preflight OK: {len(selected)} fresh Codex task(s), "
        "8 read-only tools, maximum 3 calls, no answer metadata in the model payload."
    )
    if not execute:
        typer.echo(
            "No Codex tasks started. Add --execute after `codex login status` confirms "
            "ChatGPT sign-in."
        )
        return
    try:
        output, summary = run_isolated_suite(
            root,
            path,
            limit=limit,
            question_id=question_id,
            workers=workers,
        )
    except Exception as exc:
        typer.echo(f"Blind evaluation failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    echo_json({"output": str(output), "summary": summary})


@app.command("query-report")
def query_report_command(
    vault: VaultOption = Path("."),
) -> None:
    log_file = vault / ".synapse" / "query-log.jsonl"
    if not log_file.exists():
        typer.echo("Query log is empty or does not exist.")
        return
        
    zero_hit_counts = {}
    op_counts = {}
    durations = []
    substring_fallback_count = 0
    
    try:
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                    
                op = record.get("op", "unknown")
                op_counts[op] = op_counts.get(op, 0) + 1
                
                if "duration_ms" in record:
                    durations.append(record["duration_ms"])
                    
                if record.get("fallback") == "substring":
                    substring_fallback_count += 1
                    
                if record.get("zero_hit"):
                    params = record.get("params") or {}
                    q_text = ""
                    if op == "find":
                        q_text = params.get("text") or ""
                    elif op == "search":
                        q_text = params.get("q") or ""
                    elif op == "query":
                        q_text = params.get("question") or ""
                    else:
                        q_text = f"[{op}] {json.dumps(params, sort_keys=True)}"
                    
                    if q_text:
                        zero_hit_counts[q_text] = zero_hit_counts.get(q_text, 0) + 1
    except Exception as exc:
        typer.echo(f"Error reading query log: {exc}", err=True)
        raise typer.Exit(1) from exc
        
    top_zero_hits = sorted(zero_hit_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    
    if durations:
        s_dur = sorted(durations)
        n = len(s_dur)
        if n % 2 == 1:
            median_duration = float(s_dur[n // 2])
        else:
            median_duration = (s_dur[n // 2 - 1] + s_dur[n // 2]) / 2.0
    else:
        median_duration = 0.0
        
    typer.echo("Query Log Report")
    typer.echo("================")
    typer.echo(f"Median Duration: {median_duration:.1f} ms")
    typer.echo(f"Substring Fallbacks: {substring_fallback_count}")
    typer.echo("")
    
    typer.echo("Operation Volume:")
    typer.echo("-----------------")
    for op, cnt in sorted(op_counts.items(), key=lambda x: x[1], reverse=True):
        typer.echo(f"{op:<12} | {cnt}")
    typer.echo("")
    
    typer.echo("Top Zero-Hit Queries:")
    typer.echo("---------------------")
    if not top_zero_hits:
        typer.echo("(none)")
    for i, (q_text, cnt) in enumerate(top_zero_hits, 1):
        typer.echo(f"{i:2d}. {q_text!r} ({cnt} count)")
    if top_zero_hits:
        typer.echo("\nconsider turning recurring zero-hits into eval questions")


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


@app.command("stats")
def stats_command(
    vault: VaultOption = Path("."),
    timing: Annotated[
        bool,
        typer.Option(
            "--timing",
            help=(
                "Measure wall times for parse_vault, no-change reindex, "
                "and neighbors me --depth 2."
            ),
        ),
    ] = False,
) -> None:
    """Show vault entity/relation counts and optional timing measurements."""
    from synapse.config import resolve_vault
    from synapse.parser import parse_vault as _parse_vault

    root = resolve_vault(vault)
    result: dict = {}

    # ── Static counts ────────────────────────────────────────────────────────
    _reindex_result = reindex(root)
    conn = connect(root)
    try:
        entity_rows = conn.execute(
            "SELECT type, review_status, COUNT(*) AS c "
            "FROM entities GROUP BY type, review_status ORDER BY type, review_status"
        ).fetchall()
        relation_rows = conn.execute(
            "SELECT type, COUNT(*) AS c FROM relations GROUP BY type ORDER BY type"
        ).fetchall()

        counts_by_type: dict[str, dict[str, int]] = {}
        for row in entity_rows:
            et = row["type"]
            rs = row["review_status"]
            counts_by_type.setdefault(et, {})
            counts_by_type[et][rs] = row["c"]

        total_entities = conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        total_verified = conn.execute(
            "SELECT COUNT(*) AS c FROM entities WHERE review_status IN ('verified')"
        ).fetchone()["c"]
        total_proposed = conn.execute(
            "SELECT COUNT(*) AS c FROM entities WHERE review_status='proposed'"
        ).fetchone()["c"]
        total_relations = conn.execute("SELECT COUNT(*) AS c FROM relations").fetchone()["c"]

        rel_counts_by_type: dict[str, int] = {row["type"]: row["c"] for row in relation_rows}
    finally:
        conn.close()

    result["entities"] = {
        "total": total_entities,
        "verified": total_verified,
        "proposed": total_proposed,
        "by_type": counts_by_type,
    }
    result["relations"] = {
        "total": total_relations,
        "by_type": rel_counts_by_type,
    }

    # ── Performance thresholds ────────────────────────────────────────────────
    thresholds = {
        "entity_count_trigger": 5000,
        "no_change_reindex_trigger_s": 2.0,
        "neighbors_depth2_trigger_s": 1.0,
    }
    result["thresholds"] = thresholds
    result["above_threshold"] = {
        "entity_count": total_entities > thresholds["entity_count_trigger"],
    }

    # ── Optional timing ───────────────────────────────────────────────────────
    if timing:
        import time as _time

        # 1. parse_vault wall time
        t0 = _time.perf_counter()
        _parse_vault(root)
        parse_vault_s = _time.perf_counter() - t0

        # 2. No-change reindex wall time (run immediately after a fresh reindex
        #    so the hashes haven't changed — this is the "idle" overhead)
        reindex(root, full=False)  # warm: ensure index is current
        t0 = _time.perf_counter()
        reindex(root, full=False)
        no_change_reindex_s = _time.perf_counter() - t0

        # 3. neighbors me --depth 2 wall time
        from synapse.queries import neighbors as _neighbors

        t0 = _time.perf_counter()
        _neighbors(root, "me", depth=2, reindex=False)
        neighbors_depth2_s = _time.perf_counter() - t0

        timing_data = {
            "parse_vault_s": round(parse_vault_s, 4),
            "no_change_reindex_s": round(no_change_reindex_s, 4),
            "neighbors_me_depth2_s": round(neighbors_depth2_s, 4),
        }
        result["timing"] = timing_data
        result["above_threshold"]["no_change_reindex"] = (
            no_change_reindex_s > thresholds["no_change_reindex_trigger_s"]
        )
        result["above_threshold"]["neighbors_depth2"] = (
            neighbors_depth2_s > thresholds["neighbors_depth2_trigger_s"]
        )

    echo_json(result)


@app.command("proposals")
def proposals_command(
    vault: VaultOption = Path("."),
) -> None:
    """List pending proposals in the vault with op counts and confidence."""
    from synapse.proposals import load_proposal
    root = Path(vault).resolve()
    pending_dir = root / "proposals" / "pending"
    if not pending_dir.exists():
        typer.echo("No pending proposals found (pending/ directory does not exist).")
        return
        
    pending_files = sorted(pending_dir.glob("*.yaml")) + sorted(pending_dir.glob("*.yml"))
    pending_files = sorted(list(set(pending_files)))
    if not pending_files:
        typer.echo("No pending proposals.")
        return
        
    for p_path in pending_files:
        try:
            prop = load_proposal(p_path)
            op_count = len(prop.ops)
            safe_echo(f"{p_path.name} | confidence: {prop.confidence:<6} | {op_count} ops | rationale: {prop.rationale.strip()}")
        except Exception as exc:
            safe_echo(f"{p_path.name} | [Error parsing proposal: {exc}]")


@app.command("apply")
def apply_command(
    proposal_path: Annotated[Path, typer.Argument(help="Path to the proposal YAML file.")],
    vault: VaultOption = Path("."),
    execute: Annotated[bool, typer.Option("--execute", help="Execute the proposal's operations.")] = False,
    allow_verify: Annotated[bool, typer.Option("--allow-verify", help="Print recommended verify command lines.")] = False,
) -> None:
    """Apply a proposal in dry-run mode by default, or with --execute."""
    from synapse.proposals import apply_proposal
    
    root = Path(vault).resolve()
    res = apply_proposal(root, proposal_path, execute=execute, allow_verify=allow_verify)
    if not res["success"]:
        typer.echo("Error applying proposal:", err=True)
        for err in res.get("errors", []):
            typer.echo(f"  - {err}", err=True)
        if res.get("stale"):
            raise typer.Exit(1)
        raise typer.Exit(2)
        
    if execute:
        typer.echo("Proposal applied successfully.")
        echo_json({"results": res["results"]})


@app.command("career-report")
def career_report_command(
    vault: VaultOption = Path("."),
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write report Markdown to this file.")
    ] = None,
) -> None:
    report = generate_career_report(vault)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        echo_json({"output": str(output)})
    else:
        safe_echo(report)


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
    result = embed_entities(vault, force_all=all_entities_flag)
    echo_json({
        "embedded": result["embedded"],
        "skipped": result.get("skipped", 0),
        "total": result.get("total", result["embedded"]),
        "model": result["model"],
    })


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="Text to search.")],
    vault: VaultOption = Path("."),
    limit: Annotated[int, typer.Option("--limit", help="Max results.")] = 15,
    text_only: Annotated[bool, typer.Option("--text-only")] = False,
    semantic_only: Annotated[bool, typer.Option("--semantic-only")] = False,
    entity_type: Annotated[str | None, typer.Option("--type", help="Entity type, such as insight or project.")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from synapse.search import hybrid_search
    result = hybrid_search(
        vault, query, limit=limit, text_only=text_only,
        semantic_only=semantic_only, entity_type=entity_type,
    )
    conn = connect(vault)
    try:
        for item in result["results"]:
            notice = format_knowledge_notice(conn, item["id"], budget_chars=600)
            if notice:
                item["knowledge_notice"] = notice
    finally:
        conn.close()
    if json_out:
        echo_json(result)
        return
    semantic_note = result.get("semantic", "ok")
    if semantic_note.startswith("refused:"):
        pass  # already printed to stderr in hybrid_search
    for item in result["results"]:
        legs = "+".join(item.get("legs", []))
        snippet = item.get("snippet", "")
        if item.get("knowledge_notice"):
            snippet = item["knowledge_notice"] + "\n" + snippet
        try:
            typer.echo(f"**{item['name']}** ({item['type']}) [{legs}] — {snippet} [{item['id']}]")
        except UnicodeEncodeError:
            safe_echo(f"{item['name']} ({item['type']}) [{legs}] -- {snippet} [{item['id']}]")
    if not result["results"]:
        typer.echo("(no results)")


@app.command("watch")
def watch_command(
    vault: VaultOption = Path("."),
    interval: Annotated[float, typer.Option("--interval", help="Polling interval seconds.")] = 2.0,
) -> None:
    _ = vault, interval
    typer.echo(
        "The autonomous ingest watcher is disabled: Digital Synapse has no API-key runtime. "
        "Use Codex to review inbox files and create proposals.",
        err=True,
    )
    raise typer.Exit(2)


@app.command("brief")
def brief_command(
    ref: Annotated[
        str | None, typer.Argument(help="Entity ref (name, id, alias). Omit for owner brief.")
    ] = None,
    vault: VaultOption = Path("."),
    budget: Annotated[
        int, typer.Option("--budget", help="Token budget (chars/4 estimate).")
    ] = 8000,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write brief to this file.")
    ] = None,
) -> None:
    reindex(vault)
    conn = connect(vault)
    try:
        start_time = time.perf_counter()
        if ref and ref != "me":
            matches = resolve_entity_ref(conn, ref)
            if not matches:
                typer.echo(f"No entity found for ref: {ref!r}", err=True)
                raise typer.Exit(1)
            if len(matches) > 1:
                safe_echo(f"Ambiguous ref {ref!r} — candidates:", err=True)
                for m in matches:
                    safe_echo(f"  {m['name']} ({m['type']}) `{m['id']}`", err=True)
                raise typer.Exit(1)
            entity_budget = budget if budget != 8000 else 4000
            text = build_entity_brief(conn, matches[0]["id"], budget_tokens=entity_budget)
        else:
            text = build_owner_brief(conn, budget_tokens=budget)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
    finally:
        conn.close()

    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "brief",
        "params": {"ref": ref, "budget": budget},
        "result_count": 1,
        "duration_ms": duration_ms,
        "zero_hit": False,
        "fallback": None,
    })

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = "<!-- derived file — regenerate with `synapse brief` -->\n"
        out_path.write_text(header + text, encoding="utf-8")
        echo_json({"output": str(out_path)})
    else:
        safe_echo(text)


@app.command("owner-context")
def owner_context_command(
    vault: VaultOption = Path("."),
    facet: Annotated[str | None, typer.Option("--facet", help="Topic; omit to list available context.")] = None,
    target: Annotated[str | None, typer.Option("--target", help="Existing project, goal or other entity ref.")] = None,
    as_of: Annotated[str | None, typer.Option("--as-of", help="YYYY-MM-DD; defaults to today (UTC).")] = None,
    budget: Annotated[int, typer.Option("--budget", min=1, max=32000, help="Character budget.")] = 8000,
) -> None:
    """Read relevant owner knowledge, conditions, evidence and corrections together."""
    reindex(vault)
    conn = connect(vault)
    try:
        target_id = None
        if target:
            matches = resolve_entity_ref(conn, target)
            if len(matches) != 1:
                safe_echo(f"Target must identify one entity: {target!r}", err=True)
                for match in matches[:20]:
                    safe_echo(f"{match['name']} [{match['id']}]", err=True)
                raise typer.Exit(1)
            target_id = matches[0]["id"]
        try:
            text = build_owner_context(
                conn, facet=facet, target_id=target_id, as_of=as_of, budget_chars=budget,
            )
        except ValueError as exc:
            safe_echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        safe_echo(text)
    finally:
        conn.close()


@app.command("dossier")
def dossier_command(
    ref: Annotated[str, typer.Argument(help="Entity ref (name, id, alias) — a company or a person.")],
    vault: VaultOption = Path("."),
    budget: Annotated[
        int, typer.Option("--budget", help="Token budget (chars/4 estimate).")
    ] = DEFAULT_DOSSIER_BUDGET_TOKENS,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write dossier to this file.")
    ] = None,
) -> None:
    """One-call, budgeted Markdown dossier for a company or a person.

    Company: identity, current/former contacts, owner history & overlap,
    touching conversations/opportunities/events, recommended warm entry
    points. Person: identity+role, relationship evidence, conversation
    history, everything else with provenance. Read-only — no new edges.
    """
    reindex(vault)
    conn = connect(vault)
    try:
        start_time = time.perf_counter()
        matches = resolve_entity_ref(conn, ref)
        if not matches:
            typer.echo(f"No entity found for ref: {ref!r}", err=True)
            raise typer.Exit(1)
        if len(matches) > 1:
            safe_echo(f"Ambiguous ref {ref!r} — candidates:", err=True)
            for m in matches:
                safe_echo(f"  {m['name']} ({m['type']}) `{m['id']}`", err=True)
            raise typer.Exit(1)
        entity_id = matches[0]["id"]
        entity_type = matches[0]["type"]
        if entity_type not in ("company", "person"):
            typer.echo(
                f"Entity {ref!r} is type={entity_type!r}; the dossier surface supports "
                "company and person entities only. Use `synapse brief` instead.",
                err=True,
            )
            raise typer.Exit(1)
        text = build_dossier(conn, entity_id, budget_tokens=budget)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
    finally:
        conn.close()

    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "dossier",
        "params": {"ref": ref, "budget": budget},
        "result_count": 1,
        "duration_ms": duration_ms,
        "zero_hit": False,
        "fallback": None,
    })

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = "<!-- derived file — regenerate with `synapse dossier <ref>` -->\n"
        out_path.write_text(header + text, encoding="utf-8")
        echo_json({"output": str(out_path)})
    else:
        safe_echo(text)


@app.command("warm-path")
def warm_path_command(
    ref: Annotated[str, typer.Argument(help="Target entity ref (name, id, alias) — a company or a person.")],
    vault: VaultOption = Path("."),
    limit: Annotated[
        int, typer.Option("--limit", help="Max number of ranked connectors to show.")
    ] = 10,
) -> None:
    """Who is my best warm path into a company or person — and why.

    Evidence-ranked connectors from existing signals only (interaction
    depth/recency, employment at the target, shared events, introductions,
    endorsements, geography). Deterministic, additive, hand-set weights — not
    raw BFS. Every ranked candidate carries the evidence behind its score.
    Read-only — no new edges.
    """
    reindex(vault)
    conn = connect(vault)
    try:
        start_time = time.perf_counter()
        matches = resolve_entity_ref(conn, ref)
        if not matches:
            typer.echo(f"No entity found for ref: {ref!r}", err=True)
            raise typer.Exit(1)
        if len(matches) > 1:
            safe_echo(f"Ambiguous ref {ref!r} — candidates:", err=True)
            for m in matches:
                safe_echo(f"  {m['name']} ({m['type']}) `{m['id']}`", err=True)
            raise typer.Exit(1)
        entity_id = matches[0]["id"]
        entity_type = matches[0]["type"]
        entity_name = matches[0]["name"]
        if entity_type not in ("company", "person"):
            typer.echo(
                f"Entity {ref!r} is type={entity_type!r}; warm-path supports "
                "company and person entities only.",
                err=True,
            )
            raise typer.Exit(1)
        ranked = rank_connectors(conn, entity_id, limit=limit)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
    finally:
        conn.close()

    from synapse.querylog import append as log_append
    log_append(vault, {
        "iface": "cli",
        "op": "warm-path",
        "params": {"ref": ref, "limit": limit},
        "result_count": len(ranked),
        "duration_ms": duration_ms,
        "zero_hit": not ranked,
        "fallback": None,
    })

    header = f"# Warm paths into {entity_name} `{entity_id}`\n"
    if not ranked:
        safe_echo(header + "\n(no warm paths found)")
        return
    lines = [header]
    for cand in ranked:
        evidence = "; ".join(cand["evidence"])
        uv = " (unverified)" if cand["review_status"] == "proposed" else ""
        lines.append(f"- {cand['name']}{uv} `{cand['id']}` — score {cand['score']} — {evidence}")
    safe_echo("\n".join(lines))


@app.command("agent-guide")
def agent_guide_command(
    vault: VaultOption = Path("."),
) -> None:
    out = write_agent_guide(vault)
    echo_json({"output": str(out)})


@app.command("verify")
def verify_command(
    refs: Annotated[
        list[str] | None,
        typer.Argument(help="Entity refs (names, ids, aliases) to verify."),
    ] = None,
    vault: VaultOption = Path("."),
    entity_type: Annotated[
        str | None, typer.Option("--type", help="Batch-verify all entities of this type.")
    ] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", help="Batch-verify all entities with this tag.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm batch operations without prompt.")
    ] = False,
    report: Annotated[
        bool, typer.Option("--report", help="Print verification status report and exit.")
    ] = False,
) -> None:
    if report:
        result = verify_report(vault)
        typer.echo("Verification Report")
        typer.echo("===================")
        for etype in sorted(result["counts_by_type"]):
            counts = result["counts_by_type"][etype]
            typer.echo(
                f"{etype:<20} verified: {counts.get('verified', 0):4d}  "
                f"proposed: {counts.get('proposed', 0):4d}"
            )
        advisories = result["pending_recommend_verify"]
        if advisories:
            typer.echo(f"\nPending recommend_verify advisories: {len(advisories)}")
            for adv in advisories:
                safe_echo(f"  {adv['file']}: {adv['recommend_verify']}")
        return

    if not refs and not entity_type and not tag:
        typer.echo("Provide at least one ref, --type, or --tag.", err=True)
        raise typer.Exit(1)

    try:
        result = verify_entities(
            list(refs or []),
            vault,
            type_filter=entity_type,
            tag_filter=tag,
            yes=yes,
        )
    except AmbiguousRefError as exc:
        safe_echo(f"Ambiguous ref {exc.ref!r} — candidates:", err=True)
        for c in exc.candidates:
            safe_echo(f"  {c['name']} ({c['type']}) `{c['id']}`", err=True)
        raise typer.Exit(1) from exc

    if result.get("requires_yes"):
        candidates = result.get("candidates") or []
        typer.echo(f"Will verify {len(candidates)} entities:")
        for c in candidates:
            safe_echo(f"  {c['name']} `{c['id']}`")
        typer.echo("Re-run with --yes to confirm.")
        raise typer.Exit(1)

    echo_json({
        "verified": result["verified"],
        "already_verified": result["already_verified"],
        "not_found": result["not_found"],
    })
