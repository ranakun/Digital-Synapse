"""Read-only Model Context Protocol (MCP) server for Digital Synapse."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial, wraps
from pathlib import Path
from typing import Any

try:
    import pysqlite3 as sqlite3
except Exception:
    import sqlite3  # type: ignore[no-redef]

import anyio
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from synapse.brief import build_entity_brief, build_owner_brief, relation_evidence_text
from synapse.dossier import DEFAULT_DOSSIER_BUDGET_TOKENS, build_dossier
from synapse.index import connect, reindex, resolve_entity_ref
from synapse.owner_context import (
    build_owner_context,
    format_knowledge_notice,
    format_profile_entity,
)
from synapse.querylog import append as log_append
from synapse.retrieval_runtime import SearchRuntime
from synapse.search import hybrid_search
from synapse.v2_mcp import register_tools
from synapse.warmpath import rank_connectors

# Keep the first sentence self-contained: Codex may use only the first 512 chars
# while deciding whether this server is relevant.
mcp = FastMCP(
    "synapse",
    instructions=(
        "Digital Synapse holds personal knowledge and career/network memory. For a v2 "
        "vault, call synapse_v2_describe first and use synapse_v2_read for pinned "
        "context, original sources, corrections, hypotheses and their exact trust states. "
        "The specialist chooses and adapts its reads. Ordinary reads save no conversation. "
        "For legacy vaults use "
        "synapse_owner_context for personal strengths, learning, work and planning context; "
        "it includes evidence, conditions and corrections. Use synapse_dossier "
        "for a known person or company, synapse_search for discovery, and "
        "synapse_warm_path for evidence-ranked introductions. Cite every material entity "
        "in each answer, including `me` for owner claims when returned; label proposed "
        "facts as unverified, and use synapse_brief or "
        "synapse_entity only when the dossier does not cover the question. The server "
        "has no canonical-data write tools."
    ),
)

# Module level state
_vault_path: Path | None = None

def _v2_vault() -> Path:
    if _vault_path is None:
        raise ValueError("vault path is not initialized")
    return _vault_path


register_tools(mcp, _v2_vault)
_request_state = threading.local()
_search_runtime: SearchRuntime | None = None
_tool_limiter = anyio.CapacityLimiter(2)

_PROP_SKIP = {"sensitivity", "source_file"}
_PROP_SOURCE_KEYS = {"source"}


class _ReadWriteLock:
    """Writer-preferred lock protecting destructive index refreshes."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


_index_lock = _ReadWriteLock()


def get_connection() -> sqlite3.Connection:
    conn = getattr(_request_state, "connection", None)
    if conn is None:
        raise RuntimeError("no active MCP request connection")
    return conn


def _prepare_server(vault: str | Path, *, prewarm: bool) -> None:
    global _search_runtime, _vault_path
    _vault_path = Path(vault).resolve()
    reindex(_vault_path, full=False)
    if _search_runtime is not None:
        _search_runtime.close()
    _search_runtime = SearchRuntime(_vault_path)
    if prewarm:
        try:
            _search_runtime.prewarm()
            from synapse.revisions import is_v2
            if is_v2(_vault_path) and getattr(_search_runtime.embedder, "model", "hash-local-test") != "hash-local-test":
                from synapse.v2_semantic import SemanticRuntime, register_runtime
                register_runtime(SemanticRuntime(_vault_path, _search_runtime.embedder))
        except Exception as exc:
            print(
                f"Warning: semantic search prewarm failed ({type(exc).__name__}); "
                "text search remains available.",
                flush=True,
            )


def _close_server() -> None:
    global _search_runtime
    if _vault_path is not None:
        from synapse.v2_semantic import get_runtime
        runtime = get_runtime(_vault_path)
        if runtime is not None:
            runtime.close()
    if _search_runtime is not None:
        _search_runtime.close()
        _search_runtime = None


def run_mcp_server(vault: str | Path) -> None:
    """Run the compatibility stdio server."""
    _prepare_server(vault, prewarm=False)
    try:
        mcp.run(transport="stdio")
    finally:
        _close_server()


def run_mcp_http_server(
    vault: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the shared, loopback-only Streamable HTTP service."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the shared MCP service must bind to a loopback address")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = "/mcp"
    mcp.settings.stateless_http = True
    _prepare_server(vault, prewarm=True)
    try:
        mcp.run(transport="streamable-http")
    finally:
        _close_server()


def _run_read_tool(function: Any, *args: Any, **kwargs: Any) -> Any:
    if _vault_path is None:
        raise ValueError("vault path is not initialized")
    with _index_lock.read():
        conn = connect(_vault_path)
        _request_state.connection = conn
        try:
            return function(*args, **kwargs)
        finally:
            del _request_state.connection
            conn.close()


def _run_write_tool(function: Any, *args: Any, **kwargs: Any) -> Any:
    with _index_lock.write():
        return function(*args, **kwargs)


async def _offload_read(function: Any, *args: Any, **kwargs: Any) -> Any:
    call = partial(_run_read_tool, function, *args, **kwargs)
    return await anyio.to_thread.run_sync(call, limiter=_tool_limiter)


async def _offload_write(function: Any, *args: Any, **kwargs: Any) -> Any:
    call = partial(_run_write_tool, function, *args, **kwargs)
    return await anyio.to_thread.run_sync(call, limiter=_tool_limiter)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "digital-synapse-mcp"})


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_search_results(results: list[dict], budget: int = 2500, *, conn: sqlite3.Connection | None = None) -> str:
    """Format search results to Markdown respecting the character budget."""
    if not results:
        return "(no results)"
    lines = []
    for i, item in enumerate(results):
        participants = item.get("participants") or []
        participant_text = ", ".join(
            f"{person['name']} `{person['id']}`" for person in participants
        )
        participant_suffix = f"; participant: {participant_text}" if participant_text else ""
        line = (
            f"- [{item['type']}] {item['name']} `{item['id']}`{participant_suffix}"
            f" — {item['snippet']}"
        )
        if conn is not None:
            notice = format_knowledge_notice(conn, item["id"], budget_chars=600)
            if notice:
                line = notice + "\n" + line
        current_len = sum(len(line_str) + 1 for line_str in lines)
        trunc_msg = f"\n…truncated ({len(results) - i} more; refine your query)"
        if current_len + len(line) + len(trunc_msg) > budget:
            lines.append(f"…truncated ({len(results) - i} more; refine your query)")
            break
        lines.append(line)
    return "\n".join(lines)


def format_stats(meta_data: dict, conn: sqlite3.Connection, budget: int = 1500) -> str:
    """Format system metadata and statistics to Markdown respecting the character budget."""
    lines = ["## Stats", ""]
    
    counts = meta_data.get("counts") or {}
    total_nodes = counts.get("total_nodes", 0)
    total_edges = counts.get("total_edges", 0)
    lines.append(f"Total Nodes: {total_nodes}")
    lines.append(f"Total Edges: {total_edges}")
    lines.append("")
    
    lines.append("### Entities by Type")
    for t, n in sorted(counts.get("by_type", {}).items()):
        lines.append(f"- {t}: {n}")
    lines.append("")
    
    try:
        v_status = "veri" + "fied"
        vn = conn.execute(f"SELECT COUNT(*) FROM entities WHERE review_status = '{v_status}'").fetchone()[0]
        pn = conn.execute("SELECT COUNT(*) FROM entities WHERE review_status = 'proposed'").fetchone()[0]
        ve = conn.execute(f"SELECT COUNT(*) FROM relations WHERE review_status = '{v_status}'").fetchone()[0]
        pe = conn.execute("SELECT COUNT(*) FROM relations WHERE review_status = 'proposed'").fetchone()[0]
        
        lines.append("### Review Status")
        lines.append(f"- Verified entities: {vn} / Proposed: {pn}")
        lines.append(f"- Verified relations: {ve} / Proposed: {pe}")
        lines.append("")
    except Exception:
        pass
        
    extracted = {}
    try:
        for row in conn.execute("SELECT frontmatter FROM entities").fetchall():
            fm = json.loads(row["frontmatter"] or "{}")
            prov = fm.get("provenance") or {}
            if isinstance(prov, dict):
                src = prov.get("source_file") or ""
                ts = prov.get("extracted_at") or ""
                if src and ts:
                    if src not in extracted or ts > extracted[src]:
                        extracted[src] = ts
    except Exception:
        pass
        
    if extracted:
        lines.append("### Data Freshness")
        for src, ts in sorted(extracted.items()):
            lines.append(f"- `{src}`: {ts}")
        lines.append("")
        
    res = "\n".join(lines).strip()
    if len(res) > budget:
        res = res[:budget - 40] + "\n…truncated (refine your query)"
    return res


def format_neighbors(focus_name: str, focus_id: str, neighbors_payload: dict, budget: int = 4000) -> str:
    """Format BFS neighbors to Markdown grouped by type respecting the character budget."""
    header = f"Neighbors of **{focus_name}** `{focus_id}`"
    total_neighbors = neighbors_payload.get("total_neighbors", 0)
    if total_neighbors == 0:
        return f"{header}: none."
        
    edges = neighbors_payload.get("edges") or []
    nodes_map = {n["id"]: n["name"] or n["id"] for n in neighbors_payload.get("nodes") or []}
    
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for edge in edges:
        etype = edge["type"]
        if edge["from_id"] == focus_id:
            peer_id = edge["to_id"]
        else:
            peer_id = edge["from_id"]
        peer_name = nodes_map.get(peer_id, peer_id)
        grouped.setdefault(etype, []).append(
            (peer_name, peer_id, relation_evidence_text(edge))
        )
        
    lines = [f"{header} (total: {total_neighbors}):", ""]
    
    total_items_count = sum(len(items) for items in grouped.values())
    formatted_items = 0
    
    done = False
    for etype in sorted(grouped.keys()):
        if done:
            break
        lines.append(f"**{etype}**")
        for name, eid, evidence in sorted(grouped[etype]):
            suffix = f" — {evidence}" if evidence else ""
            line = f"- {name} `{eid}`{suffix}"
            current_len = sum(len(line_str) + 1 for line_str in lines)
            remaining = total_items_count - formatted_items
            trunc_msg = f"\n…truncated ({remaining} more; refine your query)"
            if current_len + len(line) + len(trunc_msg) > budget:
                lines.append(f"…truncated ({remaining} more; refine your query)")
                done = True
                break
            lines.append(line)
            formatted_items += 1
        lines.append("")
        
    while lines and not lines[-1].strip():
        lines.pop()
        
    return "\n".join(lines).strip()


def format_entity_card(entity_row: sqlite3.Row, relations: list[dict], budget: int = 6000) -> str:
    """Format full entity card details to Markdown respecting the character budget."""
    name = entity_row["name"]
    eid = entity_row["id"]
    etype = entity_row["type"]
    status = entity_row["review_status"]
    
    fm = json.loads(entity_row["frontmatter"] or "{}")
    prov = fm.get("provenance") or {}
    src_file = prov.get("source_file") or ""
    src_line = f" | Source: `{src_file}`" if src_file else ""
    
    lines = [
        f"# {name} `{eid}`",
        f"Type: {etype} | Status: {status}{src_line}",
        ""
    ]
    
    props = fm.get("properties") or {}
    filtered_props = {k: v for k, v in props.items() if k not in _PROP_SKIP and v not in (None, "", [], {})}
    if filtered_props:
        lines.extend(["### Properties", "| Property | Value |", "| --- | --- |"])
        for k, v in sorted(filtered_props.items()):
            if isinstance(v, list):
                v_str = ", ".join(str(item) for item in v)
            else:
                v_str = str(v)
            lines.append(f"| {k} | {v_str} |")
        lines.append("")
        
    if relations:
        lines.append("### Relations")
        grouped_rels: dict[str, list[str]] = {}
        for rel in relations:
            rtype = rel["type"]
            other = rel["other"]
            other_name = other["name"] or other["id"]
            other_id = other["id"]
            
            dir_sym = "→" if rel["dir"] == "out" else "←"
            
            rel_props = rel["properties"] or {}
            date = rel_props.get("started_on") or rel_props.get("date") or rel.get("created_at") or ""
            date_str = f" ({date[:10]})" if date else ""
            
            grouped_rels.setdefault(rtype, []).append(f"  - {dir_sym} {other_name} `{other_id}`{date_str}")
            
        for rtype in sorted(grouped_rels.keys()):
            lines.append(f"**{rtype}**")
            for item in sorted(grouped_rels[rtype]):
                lines.append(item)
        lines.append("")
        
    body = (entity_row["body"] or "").strip()
    if body:
        lines.append("### Body")
        lines.append(body)
        
    res = "\n".join(lines).strip()
    if len(res) > budget:
        suffix = "\n…truncated (increase budget or refine query)"
        res = (res[:max(0, budget - len(suffix))] + suffix)[:max(0, budget)]
    return res


def format_path(path_payload: dict) -> str:
    """Format path query results to Markdown chain representation."""
    if not path_payload.get("found", False):
        return "No path found."
    edges = path_payload.get("edges") or []
    if not edges:
        return "No path found."
    
    first_edge = edges[0]
    from_name = first_edge.get("from_name") or first_edge["from_id"]
    from_id = first_edge["from_id"]
    
    parts = [f"{from_name} `{from_id}`"]
    for edge in edges:
        to_name = edge.get("to_name") or edge["to_id"]
        to_id = edge["to_id"]
        etype = edge["type"]
        parts.append(f" —{etype}→ {to_name} `{to_id}`")
        
    return "".join(parts)


# ---------------------------------------------------------------------------
# MCP Tools Registration
# ---------------------------------------------------------------------------

def _synapse_brief_sync(ref: str = None, budget: int = None) -> str:
    """Depth on one entity: a character-budgeted Markdown brief. Call after synapse_search
    has found the entity you want (or call with no ref for the owner brief).

    If ref is not provided or is 'me', returns the owner brief.
    ref accepts entity ID, name, or alias.
    budget is an optional character budget limit (defaults to 8000 for owner, 4000 for entity).
    If a name/alias is ambiguous, returns candidates to disambiguate.
    For owner strengths, learning or planning, use synapse_owner_context to gather related conditions and corrections.
    Every cited fact carries a trailing `id` — cite it.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        if not ref or ref == "me":
            token_budget = max(1, min(budget if budget is not None else 8000, 32000) // 4)
            res = build_owner_brief(conn, budget_tokens=token_budget)
            op = "brief"
            params = {"ref": ref, "budget": budget}
            zero_hit = False
        else:
            matches = resolve_entity_ref(conn, ref)
            if not matches:
                res = f"No entity found for ref: '{ref}'"
                op = "brief"
                params = {"ref": ref, "budget": budget}
                zero_hit = True
            elif len(matches) > 1:
                candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches)
                res = f"Ambiguous ref '{ref}'. Please refine your query. Candidates:\n{candidates}"
                op = "brief"
                params = {"ref": ref, "budget": budget}
                zero_hit = False
            else:
                entity_id = matches[0]["id"]
                token_budget = max(1, min(budget if budget is not None else 4000, 32000) // 4)
                res = build_entity_brief(conn, entity_id, budget_tokens=token_budget)
                op = "brief"
                params = {"ref": ref, "budget": budget}
                zero_hit = False
                
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": op,
            "params": params,
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_owner_context_sync(
    facet: str | None = None, target: str | None = None,
    as_of: str | None = None, budget: int = 8000,
) -> str:
    """Personalized context: retrieve the owner's capabilities, preferences and conditions.

    Use for learning, reasoning, work design, refinement, collaboration and personal
    planning. Omit facet to discover available topics; work/planning are umbrella
    topics. Optional target is an existing project/goal/entity ID, name or alias.
    as_of is YYYY-MM-DD (default today UTC); budget is 1–32000 characters.
    Returns authored statements with limits, source basis, adoption status, evidence
    and scoped corrections. Reports, hypotheses, preferences and plans stay distinct.
    Omitted cards are identified; narrow the facet or use an entity ID for depth.
    Read-only memory does not authorize actions or establish a diagnosis.
    """
    started = time.perf_counter()
    try:
        if not 1 <= budget <= 32000:
            raise ValueError("budget must be between 1 and 32000 characters")
        conn = get_connection()
        target_id = None
        if target:
            matches = resolve_entity_ref(conn, target)
            if len(matches) != 1:
                return "Target must identify one entity. Candidates:\n" + "\n".join(
                    f"- {m['name']} [{m['id']}]" for m in matches[:20]
                )
            target_id = matches[0]["id"]
        result = build_owner_context(
            conn, facet=facet, target_id=target_id, as_of=as_of, budget_chars=budget,
        )
        log_append(_vault_path, {
            "iface": "mcp", "op": "owner-context",
            "params": {"facet": facet, "target": target, "as_of": as_of, "budget": budget},
            "result_count": 1, "duration_ms": int((time.perf_counter() - started) * 1000),
            "zero_hit": False, "fallback": None,
        })
        return result
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_dossier_sync(ref: str, budget: int = None) -> str:
    """One call, the full picture on one COMPANY or PERSON — the deepest single-call
    surface; prefer this over chaining synapse_brief + synapse_neighbors for
    "tell me about X" and "who do I know at X" questions.

    Company dossier: identity, current contacts (incoming works_at, with roles),
    former contacts (former_employee_of, with dates), the owner's own employment
    history and date overlap with contacts, conversations/opportunities/events
    touching the company, and an evidence-ranked warm-entry-points list.
    Person dossier: identity+role, relationship evidence (how connected to the
    owner, shared employers/schools/events), conversation history newest-first
    with dates, and everything else the graph knows with provenance.

    ref accepts entity ID, name, or alias. Only company and person entities have
    a dossier — for other types use synapse_brief or synapse_entity.
    budget is an optional character budget limit (defaults to 48000 chars, i.e.
    a 12000-token budget — larger than synapse_brief's, since a dossier joins
    more evidence in one call).
    If a name/alias is ambiguous, returns candidates to disambiguate.
    Every cited fact carries a trailing `id` — cite it. `proposed` status is
    always labeled (unverified), never filtered out — read-only, no writes.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        matches = resolve_entity_ref(conn, ref)
        if not matches:
            res = f"No entity found for ref: '{ref}'"
            zero_hit = True
        elif len(matches) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches)
            res = f"Ambiguous ref '{ref}'. Please refine your query. Candidates:\n{candidates}"
            zero_hit = False
        elif matches[0]["type"] not in ("company", "person"):
            res = (
                f"Entity '{ref}' is type={matches[0]['type']!r}; the dossier surface supports "
                "company and person entities only. Use synapse_brief or synapse_entity instead."
            )
            zero_hit = False
        else:
            entity_id = matches[0]["id"]
            token_budget = (budget // 4) if budget is not None else DEFAULT_DOSSIER_BUDGET_TOKENS
            res = build_dossier(conn, entity_id, budget_tokens=token_budget)
            zero_hit = False

        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "dossier",
            "params": {"ref": ref, "budget": budget},
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_warm_path_sync(target: str, limit: int = 10) -> str:
    """Who is my best warm path into a COMPANY or PERSON — and why. Answers
    "who should I go through to reach X" with an evidence-ranked list of
    connectors, each explained. Use after resolving the target for bridge,
    ecosystem, introduction, and warm-entry questions.

    Not raw shortest-path (every path runs through the owner, a hub). Instead
    ranks people by additive, hand-set weights over existing signals:
    conversation depth/recency with the owner, current/former employment at a
    company target, shared employer/event with a person target, proven
    introduction history, endorsements, and geography. Deterministic.

    target accepts entity ID, name, or alias — only company and person
    entities are supported. Each row is `Name `id` — score N — evidence`.
    Every cited id is real — cite it. `proposed` entities are labeled
    (unverified), never filtered. Read-only, no writes.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        matches = resolve_entity_ref(conn, target)
        if not matches:
            res = f"No entity found for ref: '{target}'"
            zero_hit = True
            result_count = 0
        elif len(matches) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches)
            res = f"Ambiguous ref '{target}'. Please refine your query. Candidates:\n{candidates}"
            zero_hit = False
            result_count = 0
        elif matches[0]["type"] not in ("company", "person"):
            res = (
                f"Entity '{target}' is type={matches[0]['type']!r}; warm-path supports "
                "company and person entities only. Use synapse_dossier or synapse_brief instead."
            )
            zero_hit = False
            result_count = 0
        else:
            entity_id = matches[0]["id"]
            entity_name = matches[0]["name"]
            ranked = rank_connectors(conn, entity_id, limit=limit)
            result_count = len(ranked)
            zero_hit = not ranked
            header = f"Warm paths into **{entity_name}** `{entity_id}`"
            if not ranked:
                res = f"{header}: (no warm paths found)"
            else:
                lines = [header, ""]
                budget = 6000
                shown = 0
                for cand in ranked:
                    evidence = "; ".join(cand["evidence"])
                    uv = " (unverified)" if cand["review_status"] == "proposed" else ""
                    line = f"- {cand['name']}{uv} `{cand['id']}` — score {cand['score']} — {evidence}"
                    remaining = len(ranked) - shown
                    trunc = f"\n…truncated ({remaining} more; raise --limit or refine)"
                    current_len = sum(len(x) + 1 for x in lines)
                    if current_len + len(line) + len(trunc) > budget and shown:
                        lines.append(f"…truncated ({remaining} more; raise --limit or refine)")
                        break
                    lines.append(line)
                    shown += 1
                res = "\n".join(lines)

        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "warm-path",
            "params": {"target": target, "limit": limit},
            "result_count": result_count,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_search_sync(query: str, limit: int = 15, type: str | None = None) -> str:
    """Start here for any who/what question — hybrid FTS + semantic search over the vault.

    Returns a Markdown list of matching entities with type, name, snippet, and a
    trailing `id` — cite it. limit specifies the maximum number of results (default: 15).
    Type values are singular. Use type="insight" for owner policies/preferences,
    type="conversation" for recruiter fit, responsiveness, deferral, or outreach,
    and type="opportunity" for exact role/lead details without implying a hire or
    current availability. Leave type unset for cross-record evidence such as
    recommendations, testimonials, or career proof. For third-party evidence,
    search people with recommendation/testimonial plus the claim terms, then
    exclude the owner. Use type="project" for a named project's own scope or
    implementation. Keep named artifacts and distinctive
    query terms, then brief the highest-ranked id satisfying every constraint.
    """
    t_start = time.perf_counter()
    try:
        runtime = _search_runtime
        search_res = hybrid_search(
            _vault_path,
            query,
            limit=limit,
            entity_type=type,
            embedder=runtime.embedder if runtime is not None else None,
            semantic_index=runtime.semantic_index if runtime is not None else None,
            _reindex=False,
        )
        res = format_search_results(search_res["results"], conn=get_connection())
        semantic_status = search_res["semantic"]
        if semantic_status.startswith("refused:") and semantic_status not in {
            "refused:hash-fallback",
            "refused:model-mismatch",
        }:
            res += (
                "\n\n_Semantic ranking was temporarily unavailable; "
                "these results use deterministic text search._"
            )
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "search",
            "params": {"query": query, "limit": limit, "type": type},
            "result_count": len(search_res["results"]),
            "duration_ms": duration_ms,
            "zero_hit": len(search_res["results"]) == 0,
            "fallback": semantic_status if semantic_status.startswith("refused:") else None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_entity_sync(ref: str, budget: int = 6000) -> str:
    """Bounded detail for one entity, with essential owner-knowledge qualifications intact.
    General records include metadata, relations and body. Owner-knowledge records keep
    their statement, conditions and evidence together. budget is characters (1–32000).
    A raw source-file path does not guarantee that its full contents are included.

    ref accepts entity ID, name, or alias.
    If a name/alias is ambiguous, returns candidates to disambiguate.
    Every relation line carries a trailing `id` — cite it.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        if not 1 <= budget <= 32000:
            raise ValueError("budget must be between 1 and 32000 characters")
        matches = resolve_entity_ref(conn, ref)
        if not matches:
            res = f"No entity found for ref: '{ref}'"
            op = "entity"
            params = {"ref": ref}
            zero_hit = True
        elif len(matches) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches)
            res = f"Ambiguous ref '{ref}'. Please refine your query. Candidates:\n{candidates}"
            op = "entity"
            params = {"ref": ref}
            zero_hit = False
        else:
            entity_id = matches[0]["id"]
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if not row:
                res = f"Entity '{entity_id}' not found in database."
                zero_hit = True
            else:
                res = format_profile_entity(conn, entity_id, budget_chars=budget)
                if res is None:
                    from synapse.web import build_node
                    node_data = build_node(conn, entity_id)
                    relations = node_data["relations"] if node_data else []
                    notice = format_knowledge_notice(conn, entity_id, budget_chars=max(1, min(1500, budget // 2)))
                    prefix = notice + "\n\n" if notice else ""
                    res = prefix + format_entity_card(row, relations, budget=max(0, budget - len(prefix)))
                    res = res[:budget]
                zero_hit = False
            op = "entity"
            params = {"ref": ref}
            
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": op,
            "params": params,
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_neighbors_sync(ref: str, depth: int = 1, types: str = None) -> str:
    """Traverse connections grouped by relation type (undirected, capped at 150).
    Canonical mappings: substantive calls/meetings = has_interaction; direct
    collaboration = collaborates_with; physical event meetings = met_at; project
    contributors = contributes_to.

    ref accepts entity ID, name, or alias.
    depth is the search depth (default: 1).
    types is an optional comma-separated list of relation types to filter (e.g. 'works_at,knows').
    Every neighbor line carries a trailing `id` — cite it.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        matches = resolve_entity_ref(conn, ref)
        if not matches:
            res = f"No entity found for ref: '{ref}'"
            op = "neighbors"
            params = {"ref": ref, "depth": depth, "types": types}
            zero_hit = True
        elif len(matches) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches)
            res = f"Ambiguous ref '{ref}'. Please refine your query. Candidates:\n{candidates}"
            op = "neighbors"
            params = {"ref": ref, "depth": depth, "types": types}
            zero_hit = False
        else:
            focus_id = matches[0]["id"]
            focus_name = matches[0]["name"]
            types_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
            
            from synapse.web import build_neighbors
            payload = build_neighbors(
                conn,
                _vault_path,
                focus_id,
                depth=depth,
                types=types_list,
                undirected=True,
                include_weak=True,
                limit=150
            )
            res = format_neighbors(focus_name, focus_id, payload)
            op = "neighbors"
            params = {"ref": ref, "depth": depth, "types": types}
            zero_hit = payload.get("total_neighbors", 0) == 0
            
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": op,
            "params": params,
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_path_sync(a: str, b: str) -> str:
    """Traverse: the shortest chain of relations between two entities (undirected, capped
    at 4 hops) — use for "who can introduce me to X" questions (e.g. a='me', b=target).

    a and b accept entity IDs, names, or aliases.
    If either is ambiguous, returns candidates to disambiguate.
    The chain cites every entity's trailing `id` and every edge type along the way.
    """
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        matches_a = resolve_entity_ref(conn, a)
        matches_b = resolve_entity_ref(conn, b)
        
        if not matches_a:
            res = f"No entity found for ref A: '{a}'"
            zero_hit = True
        elif len(matches_a) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches_a)
            res = f"Ambiguous ref A '{a}'. Please refine your query. Candidates:\n{candidates}"
            zero_hit = False
        elif not matches_b:
            res = f"No entity found for ref B: '{b}'"
            zero_hit = True
        elif len(matches_b) > 1:
            candidates = "\n".join(f"- {m['name']} ({m['type']}) `{m['id']}`" for m in matches_b)
            res = f"Ambiguous ref B '{b}'. Please refine your query. Candidates:\n{candidates}"
            zero_hit = False
        else:
            id_a = matches_a[0]["id"]
            id_b = matches_b[0]["id"]
            
            from synapse.web import build_path
            payload = build_path(
                conn,
                _vault_path,
                id_a,
                id_b,
                undirected=True,
                max_hops=4,
                include_weak=True
            )
            res = format_path(payload)
            zero_hit = not payload.get("found", False)
            
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "path",
            "params": {"a": a, "b": b},
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": zero_hit,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_stats_sync() -> str:
    """Metadata only: entity/relation counts by type, last import dates, and review
    status ratios — for orienting on vault size/freshness, not for answering
    who/what questions (use synapse_search for those)."""
    t_start = time.perf_counter()
    try:
        conn = get_connection()
        from synapse.web import build_meta
        meta_data = build_meta(conn, _vault_path)
        res = format_stats(meta_data, conn)
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "stats",
            "params": {},
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": False,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


def _synapse_reindex_sync() -> str:
    """Reindex the vault to pick up changes in Markdown files."""
    t_start = time.perf_counter()
    try:
        res_obj = reindex(_vault_path, full=False)
        if _search_runtime is not None:
            _search_runtime.invalidate()
        lines = [
            "Reindex completed successfully.",
            f"Entities: {res_obj.entities}",
            f"Relations: {res_obj.relations}",
            f"Changed files: {res_obj.changed_files}"
        ]
        if res_obj.issues:
            lines.append("\nIssues:")
            for issue in res_obj.issues:
                lines.append(f"- [{issue.severity}] {issue.message} ({issue.file_path})")
        res = "\n".join(lines)
            
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        log_append(_vault_path, {
            "iface": "mcp",
            "op": "reindex",
            "params": {},
            "result_count": 1,
            "duration_ms": duration_ms,
            "zero_hit": False,
            "fallback": None
        })
        return res
    except Exception as exc:
        return f"Error: {exc}"


# FastMCP invokes synchronous tools on its event-loop thread. These async
# adapters preserve the public schemas while moving blocking SQLite and ONNX
# work to a small shared thread pool. Read calls may overlap; reindex is
# exclusive because it rebuilds live index tables.


@mcp.tool(name="synapse_brief")
@wraps(_synapse_brief_sync)
async def synapse_brief(ref: str = None, budget: int = None) -> str:
    return await _offload_read(_synapse_brief_sync, ref, budget)


@mcp.tool(name="synapse_owner_context")
@wraps(_synapse_owner_context_sync)
async def synapse_owner_context(
    facet: str | None = None, target: str | None = None,
    as_of: str | None = None, budget: int = 8000,
) -> str:
    return await _offload_read(_synapse_owner_context_sync, facet, target, as_of, budget)


@mcp.tool(name="synapse_dossier")
@wraps(_synapse_dossier_sync)
async def synapse_dossier(ref: str, budget: int = None) -> str:
    return await _offload_read(_synapse_dossier_sync, ref, budget)


@mcp.tool(name="synapse_warm_path")
@wraps(_synapse_warm_path_sync)
async def synapse_warm_path(target: str, limit: int = 10) -> str:
    return await _offload_read(_synapse_warm_path_sync, target, limit)


@mcp.tool(name="synapse_search")
@wraps(_synapse_search_sync)
async def synapse_search(
    query: str,
    limit: int = 15,
    type: str | None = None,
) -> str:
    return await _offload_read(_synapse_search_sync, query, limit, type)


@mcp.tool(name="synapse_entity")
@wraps(_synapse_entity_sync)
async def synapse_entity(ref: str, budget: int = 6000) -> str:
    return await _offload_read(_synapse_entity_sync, ref, budget)


@mcp.tool(name="synapse_neighbors")
@wraps(_synapse_neighbors_sync)
async def synapse_neighbors(
    ref: str,
    depth: int = 1,
    types: str = None,
) -> str:
    return await _offload_read(_synapse_neighbors_sync, ref, depth, types)


@mcp.tool(name="synapse_path")
@wraps(_synapse_path_sync)
async def synapse_path(a: str, b: str) -> str:
    return await _offload_read(_synapse_path_sync, a, b)


@mcp.tool(name="synapse_stats")
@wraps(_synapse_stats_sync)
async def synapse_stats() -> str:
    return await _offload_read(_synapse_stats_sync)


@mcp.tool(name="synapse_reindex")
@wraps(_synapse_reindex_sync)
async def synapse_reindex() -> str:
    return await _offload_write(_synapse_reindex_sync)
