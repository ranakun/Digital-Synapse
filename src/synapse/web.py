"""Local graph UI and HTML export."""

from __future__ import annotations

import json
import mimetypes
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from synapse.index import (
    all_relations,
    connect,
    entity_by_id,
    resolve_entity_ref,
)
from synapse.index import (
    reindex as _reindex,
)
from synapse.nlquery import execute_question
from synapse.queries import find_entities, neighbors, path_between
from synapse.web_explore import build_v2_browse, build_v2_threads
from synapse.web_landscape import build_v2_landscape
from synapse.web_v2 import (
    build_v2_area,
    build_v2_catalog,
    build_v2_graph,
    build_v2_map,
    build_v2_overview,
    build_v2_read,
    build_v2_session,
)


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


# Compact type -> colour maps for the self-contained export. Keep these in
# rough sync with the live UI (src/synapse/static/app.js); unknown/future
# types fall back to "_default", so new ingested types still render.
_EXPORT_NODE_COLORS = {
    "person": "#3b6fa0",
    "company": "#2e6b4f",
    "skill": "#7a4a9b",
    "project": "#2d6b6b",
    "goal": "#4d6b2d",
    "finance": "#6b4d2d",
    "event": "#6b2d4d",
    "opportunity": "#8a5c2e",
    "conversation": "#4a5568",
    "insight": "#5a5a3a",
    "_default": "#3a3f4a",
}
_EXPORT_NODE_BORDER = {
    "person": "#7ab8e0",
    "company": "#5eb88a",
    "skill": "#c08de8",
    "project": "#5eb8b8",
    "goal": "#8ab85e",
    "finance": "#b8905e",
    "event": "#b85e8a",
    "opportunity": "#d4a060",
    "conversation": "#8090a8",
    "insight": "#a8a86a",
    "_default": "#7a8494",
}
_EXPORT_EDGE_COLORS = {
    "knows": "#7cc7ff",
    "works_at": "#5eb88a",
    "former_employee_of": "#d4a060",
    "recruits_for": "#f87171",
    "demonstrates_skill": "#c084fc",
    "mentioned_in": "#6b7280",
    "_default": "#5b6674",
}


def export_html(vault: str | Path | None, query: str, output: str | Path) -> Path:
    """Render a query result to a self-contained, offline HTML graph file.

    The vendored force-graph library and the subgraph data are inlined, so the
    output file renders a real graph without any network access.
    """
    graph = execute_question(query, vault=vault)
    lib = static_dir() / "vendor" / "force-graph.min.js"
    cyto_src = lib.read_text(encoding="utf-8") if lib.exists() else ""
    graph_json = json.dumps(graph, ensure_ascii=False)
    node_colors = json.dumps(_EXPORT_NODE_COLORS)
    node_borders = json.dumps(_EXPORT_NODE_BORDER)
    edge_colors = json.dumps(_EXPORT_EDGE_COLORS)
    node_count = len(graph.get("nodes", []))
    edge_count = len(graph.get("edges", []))
    query_safe = query.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Digital Synapse Export</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: Inter, system-ui, Arial, sans-serif; margin: 0; background: #0e1116; color: #e5edf6; overflow: hidden; }}
    header {{ padding: 12px 16px; border-bottom: 1px solid #27303b; background: #0c0f14; }}
    header h1 {{ font-size: 15px; margin: 0 0 4px; }}
    header .meta {{ font-size: 12px; color: #9aa7b6; }}
    #graph {{ height: calc(100vh - 64px); width: 100vw; }}
    #empty {{ padding: 24px; color: #9aa7b6; white-space: pre-wrap; }}
  </style>
  <script>{cyto_src}</script>
</head>
<body>
  <header>
    <h1>Digital Synapse — {query_safe}</h1>
    <div class="meta">{node_count} nodes, {edge_count} edges · offline export</div>
  </header>
  <div id="graph"></div>
  <div id="empty" hidden></div>
  <script>
    const graph = {graph_json};
    const NODE_COLORS = {node_colors};
    const NODE_BORDERS = {node_borders};
    const EDGE_COLORS = {edge_colors};
    
    function nodeColor(t) {{ return NODE_COLORS[t] || NODE_COLORS._default; }}
    function nodeBorder(t) {{ return NODE_BORDERS[t] || NODE_BORDERS._default; }}
    function edgeColor(t) {{ return EDGE_COLORS[t] || EDGE_COLORS._default; }}

    if (window.ForceGraph && graph.nodes && graph.nodes.length) {{
      const elements = {{
        nodes: graph.nodes || [],
        links: (graph.edges || []).map(e => ({{
          id: String(e.id || (e.from_id + '-' + e.to_id + '-' + e.type)),
          source: e.from_id,
          target: e.to_id,
          type: e.type,
          weak: e.weak ? 1 : 0
        }}))
      }};

      // Compute degrees
      const degrees = {{}};
      elements.links.forEach(l => {{
        degrees[l.source] = (degrees[l.source] || 0) + 1;
        degrees[l.target] = (degrees[l.target] || 0) + 1;
      }});
      elements.nodes.forEach(n => {{
        n.degree = degrees[n.id] || 0;
      }});

      const myGraph = ForceGraph()(document.getElementById('graph'))
        .graphData(elements)
        .nodeId('id')
        .nodeCanvasObject((node, ctx, globalScale) => {{
          const size = Math.max(4, 3 + (node.degree || 0) * 0.8);
          const color = nodeColor(node.type);
          const border = nodeBorder(node.type);
          
          ctx.beginPath();
          ctx.arc(node.x, node.y, size, 0, 2 * Math.PI, false);
          ctx.fillStyle = color;
          ctx.fill();
          ctx.lineWidth = 1.5 / globalScale;
          ctx.strokeStyle = border;
          ctx.stroke();

          const label = node.name || node.id;
          const fontSize = 10 / globalScale;
          ctx.font = `${{fontSize}}px Inter, sans-serif`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';
          ctx.fillStyle = '#e5edf6';
          ctx.fillText(label, node.x, node.y + size + 2);
        }})
        .linkColor(link => edgeColor(link.type))
        .linkWidth(1.2)
        .linkLineDash(link => link.weak ? [4, 4] : null)
        .linkDirectionalArrowLength(3.5)
        .linkDirectionalArrowRelPos(1);
    }} else {{
      const empty = document.getElementById('empty');
      empty.hidden = false;
      empty.textContent = 'No graph to render. Raw data:\\n\\n' + JSON.stringify(graph, null, 2);
    }}
  </script>
</body>
</html>
"""
    path = Path(output).resolve()
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Builder helpers – pure functions, no reindex, receive open conn
# ---------------------------------------------------------------------------

def _parse_bool(v: str | None) -> bool:
    """Return True if v is '1', 'true', or 'yes' (case-insensitive)."""
    return (v or "").lower() in {"1", "true", "yes"}


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_meta(conn: Any, vault: Path) -> dict[str, Any]:
    """Return metadata summary: owner, counts, relation_types, entity_types."""
    # owner_id
    owner_row = conn.execute(
        "SELECT id FROM entities WHERE id = 'me'"
    ).fetchone()
    if owner_row:
        owner_id: str | None = "me"
    else:
        # first entity tagged "owner"
        owner_id = None
        for row in conn.execute("SELECT id, frontmatter FROM entities").fetchall():
            fm = json.loads(row["frontmatter"] or "{}")
            tags = fm.get("tags") or []
            if "owner" in tags:
                owner_id = row["id"]
                break

    # counts by type
    type_rows = conn.execute(
        "SELECT type, COUNT(*) AS n FROM entities GROUP BY type ORDER BY type"
    ).fetchall()
    by_type = {row["type"]: row["n"] for row in type_rows}
    total_nodes = sum(by_type.values())
    total_edges = conn.execute("SELECT COUNT(*) AS c FROM relations").fetchone()["c"]

    # relation_types: grouped by (type, weak), count desc
    rel_rows = conn.execute(
        """
        SELECT type, weak, COUNT(*) AS cnt
        FROM relations
        GROUP BY type, weak
        ORDER BY cnt DESC
        """
    ).fetchall()
    relation_types = [
        {"type": row["type"], "count": row["cnt"], "weak": bool(row["weak"])}
        for row in rel_rows
    ]

    # entity_types: distinct present types
    entity_types = sorted(by_type.keys())

    return {
        "owner_id": owner_id,
        "counts": {
            "by_type": by_type,
            "total_nodes": total_nodes,
            "total_edges": total_edges,
        },
        "relation_types": relation_types,
        "entity_types": entity_types,
        "generated_at": _utc_now_iso(),
    }


def build_search(conn: Any, vault: Path, q: str, limit: int, fallback_tracker: list[str] | None = None) -> dict[str, Any]:
    """Full-text search; returns slim result list."""
    entities = find_entities(vault, q, limit=limit, reindex=False, fallback_tracker=fallback_tracker)
    results = []
    for entity in entities:
        props = entity.get("properties") or {}
        results.append(
            {
                "id": entity["id"],
                "name": entity["name"],
                "type": entity["type"],
                "review_status": entity["review_status"],
                "tags": entity.get("tags") or [],
                "current_company": props.get("current_company"),
            }
        )
    return {"results": results}


def build_node(conn: Any, node_id: str) -> dict[str, Any] | None:
    """Return node dict with provenance + all relations. Returns None if not found."""
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (node_id,)).fetchone()
    if not row:
        return None

    fm = json.loads(row["frontmatter"] or "{}")
    node: dict[str, Any] = {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "file_path": row["file_path"],
        "review_status": row["review_status"],
        "aliases": fm.get("aliases") or [],
        "tags": fm.get("tags") or [],
        "properties": fm.get("properties") or {},
        "provenance": fm.get("provenance"),
    }

    # fetch all relations touching this entity
    rels_all = all_relations(conn, include_weak=True)
    relations = []
    for rel in rels_all:
        if rel["from_id"] == node_id:
            direction = "out"
            other_id = rel["to_id"]
        elif rel["to_id"] == node_id:
            direction = "in"
            other_id = rel["from_id"]
        else:
            continue
        other = entity_by_id(conn, other_id)
        relations.append(
            {
                "id": rel["id"],
                "dir": direction,
                "type": rel["type"],
                "weak": rel["weak"],
                "properties": rel["properties"],
                "from_name": rel.get("from_name"),
                "to_name": rel.get("to_name"),
                "other": {
                    "id": other_id,
                    "name": other["name"] if other else None,
                    "type": other["type"] if other else None,
                    "review_status": other["review_status"] if other else None,
                },
            }
        )

    return {"node": node, "relations": relations}


_STRONG_EDGE_TYPES_EXCLUSION = {"knows", "mentioned_in"}


def build_neighbors(
    conn: Any,
    vault: Path,
    node_id: str,
    depth: int,
    types: list[str] | None,
    undirected: bool,
    include_weak: bool,
    limit: int,
) -> dict[str, Any]:
    """BFS neighbors with deterministic cap/ranking."""
    result = neighbors(
        vault,
        node_id,
        depth=depth,
        relation_types=types or None,
        undirected=undirected,
        include_weak=include_weak,
        reindex=False,
    )
    all_nodes: list[dict[str, Any]] = result.nodes
    all_edges: list[dict[str, Any]] = result.edges

    # Separate focus node from neighbors
    focus = None
    neighbor_nodes: list[dict[str, Any]] = []
    for n in all_nodes:
        if n["id"] == node_id:
            focus = n
        else:
            neighbor_nodes.append(n)

    total_neighbors = len(neighbor_nodes)
    truncated = total_neighbors > limit

    if truncated:
        # Build global degree map from the relations table (include_weak same flag)
        deg_rows = conn.execute(
            """
            SELECT entity_id, COUNT(*) AS deg FROM (
                SELECT from_id AS entity_id FROM relations {weak_filter}
                UNION ALL
                SELECT to_id AS entity_id FROM relations {weak_filter}
            ) GROUP BY entity_id
            """.format(
                weak_filter="" if include_weak else "WHERE weak = 0"
            )
        ).fetchall()
        degree_map: dict[str, int] = {row["entity_id"]: row["deg"] for row in deg_rows}

        # For each neighbor, find its "connecting edge type" (pick the first edge that
        # connects it to any kept node — for depth=1 this is always the direct edge from focus).
        # Build edge index: neighbor_id -> list of edge types
        neighbor_edge_types: dict[str, list[str]] = {}
        for edge in all_edges:
            for nid in (edge["from_id"], edge["to_id"]):
                if nid != node_id:
                    neighbor_edge_types.setdefault(nid, []).append(edge["type"])

        def _rank_key(n: dict[str, Any]) -> tuple[int, int, str]:
            nid = n["id"]
            etypes = neighbor_edge_types.get(nid, [])
            # "strong" = any connecting type that is NOT in the exclusion set
            is_weak_only = all(t in _STRONG_EDGE_TYPES_EXCLUSION for t in etypes) if etypes else True
            strong_flag = 0 if not is_weak_only else 1
            deg = degree_map.get(nid, 0)
            return (strong_flag, -deg, (n.get("name") or "").lower())

        neighbor_nodes.sort(key=_rank_key)
        neighbor_nodes = neighbor_nodes[:limit]

    kept_ids: set[str] = {n["id"] for n in neighbor_nodes}
    if focus:
        kept_ids.add(focus["id"])

    # Filter edges to only reference kept nodes
    kept_edges = [
        e for e in all_edges
        if e["from_id"] in kept_ids and e["to_id"] in kept_ids
    ]

    kept_nodes = ([focus] if focus else []) + neighbor_nodes

    return {
        "nodes": kept_nodes,
        "edges": kept_edges,
        "truncated": truncated,
        "total_neighbors": total_neighbors,
    }


def build_path(
    conn: Any,
    vault: Path,
    source: str,
    target: str,
    undirected: bool,
    max_hops: int,
    include_weak: bool,
) -> dict[str, Any]:
    """Find shortest path between two entities."""
    # Resolve source/target if they look like refs rather than bare ids
    def _resolve(ref: str) -> str:
        # Check direct lookup first
        row = conn.execute("SELECT id FROM entities WHERE id = ?", (ref,)).fetchone()
        if row:
            return ref
        matches = resolve_entity_ref(conn, ref)
        if matches:
            return matches[0]["id"]
        return ref

    src_id = _resolve(source)
    tgt_id = _resolve(target)

    result = path_between(
        vault,
        src_id,
        tgt_id,
        max_hops=max_hops,
        include_weak=include_weak,
        undirected=undirected,
        reindex=False,
    )
    found = len(result.edges) > 0
    hops = len(result.edges) if found else 0
    return {
        "nodes": result.nodes,
        "edges": result.edges,
        "hops": hops,
        "found": found,
    }


def build_launcher(conn: Any) -> dict[str, Any]:
    """Return a lightweight entity list for the launcher/search bar."""
    rows = conn.execute(
        "SELECT id, type, name, review_status FROM entities ORDER BY name"
    ).fetchall()
    return {
        "entities": [
            {
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "review_status": row["review_status"],
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class SynapseHandler(BaseHTTPRequestHandler):
    vault: Path

    def _qs(self, parsed: Any) -> dict[str, list[str]]:
        return parse_qs(parsed.query)

    def _int(self, qs: dict[str, list[str]], key: str, default: int, lo: int, hi: int) -> int:
        raw = qs.get(key, [None])[0]
        try:
            v = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = self._qs(parsed)
        path = parsed.path

        # ---- /api/v2/* ----
        # v2 reads use the retained revision gateway and never touch the v1
        # index builders below.  Keep this namespace separate so existing
        # consumers retain their exact response shapes.
        if path.startswith("/api/v2/"):
            self._v2_get(path, qs)
            return

        # ---- /api/meta ----
        if path == "/api/meta":
            try:
                conn = connect(self.vault)
                try:
                    payload = build_meta(conn, self.vault)
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/search ----
        if path == "/api/search":
            try:
                import time
                q = qs.get("q", [""])[0]
                limit = self._int(qs, "limit", 20, 1, 100)
                conn = connect(self.vault)
                try:
                    start_time = time.perf_counter()
                    fallback_tracker = []
                    payload = build_search(conn, self.vault, q, limit, fallback_tracker)
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    result_count = len(payload.get("results", []))
                    zero_hit = (result_count == 0)
                    fallback = "substring" if "substring" in fallback_tracker else None
                    
                    from synapse.querylog import append as log_append
                    log_append(self.vault, {
                        "iface": "http",
                        "op": "search",
                        "params": {"q": q, "limit": limit},
                        "result_count": result_count,
                        "duration_ms": duration_ms,
                        "zero_hit": zero_hit,
                        "fallback": fallback
                    })
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/node/{id} ----
        if path.startswith("/api/node/"):
            try:
                node_id = unquote(path[len("/api/node/"):])
                conn = connect(self.vault)
                try:
                    result = build_node(conn, node_id)
                finally:
                    conn.close()
                if result is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/neighbors ----
        if path == "/api/neighbors":
            try:
                import time
                node_id = qs.get("id", [""])[0]
                depth = self._int(qs, "depth", 1, 1, 10)
                limit = self._int(qs, "limit", 250, 1, 2000)
                undirected = _parse_bool(qs.get("undirected", [None])[0])
                include_weak = _parse_bool(qs.get("include_weak", [None])[0])
                types_raw = qs.get("types", [None])[0]
                types = [t.strip() for t in types_raw.split(",") if t.strip()] if types_raw else None
                conn = connect(self.vault)
                try:
                    start_time = time.perf_counter()
                    payload = build_neighbors(
                        conn, self.vault, node_id, depth, types, undirected, include_weak, limit
                    )
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    result_count = len(payload.get("edges", []))
                    zero_hit = (result_count == 0)
                    
                    from synapse.querylog import append as log_append
                    log_append(self.vault, {
                        "iface": "http",
                        "op": "neighbors",
                        "params": {
                            "id": node_id,
                            "depth": depth,
                            "limit": limit,
                            "undirected": undirected,
                            "include_weak": include_weak,
                            "types": types,
                        },
                        "result_count": result_count,
                        "duration_ms": duration_ms,
                        "zero_hit": zero_hit,
                        "fallback": None
                    })
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/path ----
        if path == "/api/path":
            try:
                import time
                source = qs.get("source", [""])[0]
                target = qs.get("target", [""])[0]
                undirected = _parse_bool(qs.get("undirected", ["true"])[0])
                max_hops = self._int(qs, "max_hops", 4, 1, 10)
                include_weak = _parse_bool(qs.get("include_weak", [None])[0])
                conn = connect(self.vault)
                try:
                    start_time = time.perf_counter()
                    payload = build_path(conn, self.vault, source, target, undirected, max_hops, include_weak)
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    result_count = len(payload.get("edges", []))
                    zero_hit = (result_count == 0)
                    
                    from synapse.querylog import append as log_append
                    log_append(self.vault, {
                        "iface": "http",
                        "op": "path",
                        "params": {
                            "source": source,
                            "target": target,
                            "undirected": undirected,
                            "max_hops": max_hops,
                            "include_weak": include_weak,
                        },
                        "result_count": result_count,
                        "duration_ms": duration_ms,
                        "zero_hit": zero_hit,
                        "fallback": None
                    })
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/launcher ----
        if path == "/api/launcher":
            try:
                conn = connect(self.vault)
                try:
                    payload = build_launcher(conn)
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- /api/query (legacy) ----
        if path == "/api/query":
            query = qs.get("q", ["find"])[0]
            try:
                payload = execute_question(query, vault=self.vault)
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        # ---- static files ----
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        fpath = (static_dir() / relative).resolve()
        if not str(fpath).startswith(str(static_dir().resolve())) or not fpath.exists():
            self.send_response(404)
            self.end_headers()
            return
        content_type = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _v2_get(self, path: str, qs: dict[str, list[str]]) -> None:
        revision = qs.get("revision", [None])[0]
        budget_raw = qs.get("budget", ["32000" if path in {"/api/v2/map", "/api/v2/area"} else "8000"])[0]
        try:
            budget = int(budget_raw)
        except (TypeError, ValueError):
            budget = 8000
        try:
            if path == "/api/v2/session":
                payload = build_v2_session(self.vault)
            elif path == "/api/v2/overview":
                payload = build_v2_overview(self.vault, revision=revision)
            elif path == "/api/v2/landscape":
                payload = build_v2_landscape(
                    self.vault, revision=revision,
                    organization_revision=qs.get("organization_revision", [None])[0],
                    offset=self._int(qs, "offset", 0, 0, 1_000_000),
                    limit=self._int(qs, "limit", 6, 1, 6),
                    selected=qs.get("selected", [None])[0],
                    channels=qs["channels"][0].split(",") if qs.get("channels", [None])[0] else None,
                )
            elif path in {"/api/v2/browse", "/api/v2/threads"}:
                organization_revision = qs.get("organization_revision", [None])[0]
                if not organization_revision:
                    raise ValueError("Exploration requires organization_revision")
                arguments = {
                    "revision": revision,
                    "organization_revision": organization_revision,
                    "offset": self._int(qs, "offset", 0, 0, 1_000_000),
                    "query": qs.get("q", [""])[0],
                }
                if path.endswith("/browse"):
                    payload = build_v2_browse(
                        self.vault, qs.get("area_id", [""])[0],
                        group_path=qs.get("group_path", [""])[0],
                        selected=qs.get("selected", [None])[0], **arguments,
                    )
                else:
                    payload = build_v2_threads(
                        self.vault, qs.get("id", [""])[0],
                        bundle=qs.get("bundle", [""])[0], **arguments,
                    )
            elif path in {"/api/v2/map", "/api/v2/area"}:
                arguments = {
                    "revision": revision,
                    "organization_revision": qs.get("organization_revision", [None])[0],
                    "offset": self._int(qs, "offset", 0, 0, 1_000_000),
                    "limit": self._int(qs, "limit", 24 if path.endswith("/map") else 30, 1, 24 if path.endswith("/map") else 151),
                    "selected": qs.get("selected", [None])[0],
                    "channels": qs["channels"][0].split(",") if qs.get("channels", [None])[0] else None,
                    "budget_chars": budget,
                }
                if path.endswith("/map"):
                    payload = build_v2_map(self.vault, query=qs.get("q", [""])[0], **arguments)
                else:
                    if not arguments["organization_revision"]:
                        raise ValueError("Area reads require organization_revision")
                    payload = build_v2_area(self.vault, qs.get("area_id", [""])[0], **arguments)
            elif path == "/api/v2/graph":
                raw_ids = qs.get("id", []) + qs.get("ids", [])
                focus_ids = [item for raw in raw_ids for item in raw.split(",") if item]
                payload = build_v2_graph(
                    self.vault,
                    focus_ids,
                    revision=revision,
                    include_suggestions=_parse_bool(qs.get("include_suggestions", [None])[0]),
                    limit=self._int(qs, "limit", 150, 1, 150),
                    organization_revision=qs.get("organization_revision", [None])[0],
                )
            elif path == "/api/v2/catalog":
                payload = build_v2_catalog(
                    self.vault,
                    revision=revision,
                    kind=qs.get("kind", ["records"])[0],
                    subject_id=qs.get("subject_id", [None])[0],
                    facet=qs.get("facet", [None])[0],
                    availability=qs.get("availability", [None])[0],
                    query=qs.get("query", [None])[0],
                    offset=self._int(qs, "offset", 0, 0, 1_000_000),
                    limit=self._int(qs, "limit", 20, 1, 20),
                    budget_chars=budget,
                )
            elif path == "/api/v2/search":
                payload = build_v2_read(
                    self.vault,
                    "context",
                    {
                        "query": qs.get("q", [""])[0],
                        "knowledge_policy": qs.get("knowledge_policy", ["mixed"])[0],
                        "offset": self._int(qs, "offset", 0, 0, 1_000_000),
                        "limit": self._int(qs, "limit", 20, 1, 20),
                    },
                    revision=revision,
                    budget_chars=budget,
                )
            elif path == "/api/v2/read":
                operation = qs.get("operation", [""])[0]
                raw_arguments = qs.get("arguments", ["{}"])[0]
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
                payload = build_v2_read(
                    self.vault, operation, arguments, revision=revision, budget_chars=budget
                )
            else:
                operation = {
                    "/api/v2/context": "context",
                    "/api/v2/record": "record",
                    "/api/v2/source": "source",
                    "/api/v2/passage": "passage",
                }.get(path)
                if operation is None:
                    self._send_json({"error": "not found"}, status=404)
                    return
                if operation == "context":
                    arguments = {
                        "ids": qs.get("id", []),
                        "knowledge_policy": qs.get("knowledge_policy", ["mixed"])[0],
                    }
                    if not arguments["ids"]:
                        arguments.pop("ids")
                elif operation == "record":
                    arguments = {"id": qs.get("id", [""])[0], "offset": self._int(qs, "offset", 0, 0, 1_000_000), "limit": self._int(qs, "limit", 4000, 1, 16000)}
                elif operation == "source":
                    arguments = {"id": qs.get("id", [""])[0], "offset": self._int(qs, "offset", 0, 0, 1_000_000), "limit": self._int(qs, "limit", 4000, 1, 4000)}
                    if qs.get("version", [None])[0] is not None:
                        arguments["version"] = qs["version"][0]
                else:
                    try:
                        arguments = json.loads(qs.get("evidence", ["{}"])[0])
                    except json.JSONDecodeError as exc:
                        raise ValueError("evidence must be a JSON object") from exc
                    if not isinstance(arguments, dict):
                        raise ValueError("evidence must be a JSON object")
                    arguments = {"evidence": arguments}
                payload = build_v2_read(
                    self.vault, operation, arguments, revision=revision, budget_chars=budget
                )
            self._send_json(payload, status=400 if isinstance(payload, dict) and "error" in payload else 200)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        # ---- POST /api/reindex ----
        if parsed.path == "/api/reindex":
            try:
                result = _reindex(self.vault, full=False)
                self._send_json(
                    {
                        "entities": result.entities,
                        "relations": result.relations,
                        "changed_files": result.changed_files,
                    }
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(vault: str | Path | None = None, *, port: int = 7777) -> ThreadingHTTPServer:
    vault_path = Path(vault or ".").resolve()
    print("Indexing vault...")
    _reindex(vault_path, full=False)
    print("Ready.")
    handler = type(
        "ConfiguredSynapseHandler", (SynapseHandler,), {"vault": vault_path}
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.serve_forever()
    return server
