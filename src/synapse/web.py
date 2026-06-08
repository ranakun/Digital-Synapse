"""Local graph UI and HTML export."""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from synapse.index import all_entities, all_relations, connect, reindex
from synapse.nlquery import execute_question


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def export_html(vault: str | Path | None, query: str, output: str | Path) -> Path:
    graph = execute_question(query, vault=vault)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Digital Synapse Export</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; }}
    #graph {{ height: 70vh; border-top: 1px solid #ddd; }}
    pre {{ white-space: pre-wrap; padding: 16px; }}
  </style>
  <script>{(static_dir() / "cytoscape.js").read_text(encoding="utf-8") if (static_dir() / "cytoscape.js").exists() else ""}</script>
</head>
<body>
  <h1>Digital Synapse Export</h1>
  <p>{query}</p>
  <div id="graph"></div>
  <pre id="data"></pre>
  <script>
    const graph = {json.dumps(graph, ensure_ascii=False)};
    document.getElementById('data').textContent = JSON.stringify(graph, null, 2);
    if (window.cytoscape) {{
      cytoscape({{
        container: document.getElementById('graph'),
        elements: [
          ...graph.nodes.map(n => ({{ data: {{ id: n.id, label: n.name }} }})),
          ...graph.edges.map(e => ({{ data: {{ id: String(e.id || e.from_id + e.to_id), source: e.from_id, target: e.to_id, label: e.type }} }}))
        ],
        layout: {{ name: 'cose' }},
        style: [
          {{ selector: 'node', style: {{ label: 'data(label)', 'background-color': '#2563eb', color: '#111' }} }},
          {{ selector: 'edge', style: {{ label: 'data(label)', width: 2, 'line-color': '#64748b', 'target-arrow-shape': 'triangle' }} }}
        ]
      }});
    }}
  </script>
</body>
</html>
"""
    path = Path(output).resolve()
    path.write_text(html, encoding="utf-8")
    return path


class SynapseHandler(BaseHTTPRequestHandler):
    vault: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/graph":
            try:
                reindex(self.vault)
                conn = connect(self.vault)
                try:
                    payload = {
                        "nodes": all_entities(conn),
                        "edges": all_relations(conn, include_weak=True),
                    }
                finally:
                    conn.close()
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if parsed.path == "/api/query":
            query = parse_qs(parsed.query).get("q", ["find"])[0]
            try:
                payload = execute_question(query, vault=self.vault)
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        relative = "index.html" if parsed.path in {"", "/"} else parsed.path.lstrip("/")
        path = (static_dir() / relative).resolve()
        if not str(path).startswith(str(static_dir().resolve())) or not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(vault: str | Path | None = None, *, port: int = 7777) -> ThreadingHTTPServer:
    handler = type(
        "ConfiguredSynapseHandler", (SynapseHandler,), {"vault": Path(vault or ".").resolve()}
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.serve_forever()
    return server
