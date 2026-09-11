from __future__ import annotations

import copy
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from synapse import web, web_v2
from synapse.knowledge import record_descriptor
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes


def _vault(tmp_path: Path) -> tuple[Path, RevisionStore, str]:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    raw = b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOwner profile.\n"
    row = record_descriptor(raw, path="entities/people/me.md")
    descriptor, objects = prepare_source(
        "Retained source evidence café 😀 for v2 graph reading.".encode(),
        origin="synthetic.txt",
        source_id=generate_ulid(),
    )
    objects[row["version"]] = raw
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"seed"),
        mutate=lambda manifest, _read: (
            manifest["records"].update({"me": copy.deepcopy(row)}),
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
        objects=objects,
        initialize=True,
    )
    return vault, store, descriptor["id"]


def test_v2_overview_and_pinned_source_route_report_session_state(tmp_path: Path) -> None:
    vault, store, source_id = _vault(tmp_path)
    old_revision = store.head()
    summary = web.build_v2_overview(vault, revision=old_revision)
    assert summary["knowledge_revision"] == old_revision
    assert summary["sources"]["text_complete"] == 1
    assert "freshness" in summary and "limitations" in summary

    newer, objects = prepare_source(b"A newer source.", origin="new.txt", source_id=source_id)
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"new"),
        mutate=lambda manifest, _read: (
            manifest["sources"].update({source_id: newer["version"]}),
            manifest["source_versions"].update({newer["version"]: newer}),
        ),
        objects=objects,
    )
    pinned = web.build_v2_read(vault, "source", {"id": source_id}, revision=old_revision, budget_chars=3000)
    assert "Retained source evidence" in pinned["text"]
    assert pinned["evidence"]["byte_end"] - pinned["evidence"]["byte_start"] == len(pinned["text"].encode("utf-8"))
    changed = web.build_v2_overview(vault, revision=old_revision)
    assert changed["session"]["changed"] is True
    assert changed["session"]["head_revision"] == store.head()


def test_v2_graph_honors_node_edge_caps_and_preserves_edge_qualification(monkeypatch, tmp_path: Path) -> None:
    vault, store, _ = _vault(tmp_path)

    class FakeGateway:
        revision = store.head()

        def __init__(self, *_args, **_kwargs):
            self.calls = []

        def neighbors(self, identity, *, include_suggestions, limit):
            self.calls.append((identity, include_suggestions, limit))
            nodes = [{"id": identity, "name": identity, "availability": "accepted"}]
            edges = []
            for index in range(200):
                other = f"node-{index}"
                nodes.append({"id": other, "name": other, "availability": "accepted"})
                edges.append(
                    {
                        "id": f"edge-{index}",
                        "from_id": identity,
                        "to_id": other,
                        "relation": "supports",
                        "availability": "suggestion",
                        "review_status": "not-reviewed",
                        "qualified": True,
                        "requires_revalidation": False,
                        "notices": [],
                        "provisional_dependencies": [],
                        "limitations": [],
                        "context_expansion": {"method": "context", "ids": [f"assertion-{index}"]},
                    }
                )
            return {"nodes": nodes, "edges": edges, "truncated": True, "withheld_edges": 3}

    monkeypatch.setattr(web_v2, "Gateway", FakeGateway)
    result = web_v2.build_v2_graph(vault, ["focus"], include_suggestions=True, limit=150)
    assert len(result["nodes"]) == 151
    assert len(result["edges"]) <= 500
    assert result["withheld_edges"] == 3
    assert result["truncated"] is True
    assert result["knowledge_policy"] == "mixed"
    assert result["edges"][0]["qualified"] is True
    assert result["edges"][0]["context_expansion"]["method"] == "context"


def test_v2_http_namespace_isolated_from_legacy_routes(tmp_path: Path) -> None:
    vault, _, source_id = _vault(tmp_path)
    handler = type("TestV2Handler", (web.SynapseHandler,), {"vault": vault})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v2/source?id={source_id}&budget=3000"
        with urlopen(url) as response:
            payload = json.load(response)
        assert payload["source_id"] == source_id
        assert "evidence" in payload
        assert payload["knowledge_revision"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
