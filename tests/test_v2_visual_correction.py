"""Regressions for real-material constellations and the rejected focus flow."""

from __future__ import annotations

import copy
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest
from test_v2_organization import _vault

from synapse import web
from synapse.gateway import Gateway, transport_size
from synapse.organization import Organization, OrganizationConfig, _tokens, display_title
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.web_landscape import MAX_SCENE_CHARACTERS, _cloud, build_v2_landscape
from synapse.web_v2 import build_v2_area


def test_landscape_dots_are_real_qualified_members_with_exact_source_refs(tmp_path):
    vault, _ = _vault(tmp_path)
    payload = build_v2_landscape(vault)
    assert payload["clouds"]
    snapshot = Organization(Gateway(vault)).snapshot(
        organization_revision=payload["organization_revision"]
    )
    original = {m["id"]: m for m in snapshot["members"]}
    assert transport_size(payload) <= MAX_SCENE_CHARACTERS
    for cloud in payload["clouds"]:
        assert cloud["shown_material"] == len(cloud["nodes"]) <= 48
        assert cloud["shown_material"] <= cloud["total_material"]
        source_ids = []
        visible = {n["id"] for n in cloud["nodes"]}
        for node in cloud["nodes"]:
            assert node["ref"] == original[node["id"]]["ref"]
            assert node["qualification"] == original[node["id"]]["qualification"]
            if node["kind"] == "source":
                source_ids.append(node["ref"]["source_id"])
                assert node["ref"]["evidence"]
        assert len(source_ids) == len(set(source_ids))
        assert all(
            e["from"] in visible and e["to"] in visible and e["channel"] == "similarity"
            for e in cloud["edges"]
        )
    assert OrganizationConfig().community_resolution == 1.0
    assert snapshot["configuration"]["community_resolution"] == 0.2


def test_focusing_an_existing_area_is_not_reported_as_regrouping(tmp_path):
    vault, _ = _vault(tmp_path)
    landscape = build_v2_landscape(vault)
    area = landscape["nodes"][0]
    focus = build_v2_area(
        vault,
        area["id"],
        organization_revision=landscape["organization_revision"],
        selected=area["id"],
    )
    assert focus["selection"]["state"] == "available"
    assert focus["selection"]["member"]["id"] == area["id"]
    assert focus["knowledge_revision"] == landscape["knowledge_revision"]
    assert focus["organization_revision"] == landscape["organization_revision"]


def test_one_long_source_does_not_masquerade_as_several_independent_notes(tmp_path):
    vault = tmp_path / "brain"
    store = RevisionStore(vault)
    raw = "\n\n".join(
        f"Watercolor pigments and paper texture observation {i}. "
        + "Paint granulation and washes. " * 20
        for i in range(12)
    ).encode()
    descriptor, objects = prepare_source(raw, origin="watercolor-notebook.md")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(raw),
        objects=objects,
        initialize=True,
        mutate=lambda m, _: (
            m["sources"].update({descriptor["id"]: descriptor["version"]}),
            m["source_versions"].update({descriptor["version"]: descriptor}),
        ),
    )
    snapshot = Organization(Gateway(vault)).snapshot()
    assert len(snapshot["members"]) >= 3
    assert snapshot["areas"] == []
    assert snapshot["loose"]
    assert Gateway(vault).source(descriptor["id"])["text"]


def test_source_aliases_have_one_dot_and_one_visual_similarity_line():
    def member(identity, source):
        return {
            "id": identity,
            "kind": "source",
            "label": source + ".md",
            "ref": {"source_id": source, "source_version": "v", "evidence": [{"exact": identity}]},
            "area_ids": ["area:a"],
            "reasons": [],
            "qualification": {
                "state": "source-material",
                "limitations": ["Partial source"],
                "extraction_state": "partial",
            },
        }

    nodes = [member("passage:1", "left"), member("passage:2", "left"), member("passage:3", "right")]
    area = {"id": "area:a", "member_ids": [n["id"] for n in nodes]}
    links = [
        {"id": f"e{i}", "from": left, "to": "passage:3", "channel": "similarity"}
        for i, left in enumerate(["passage:1", "passage:2"])
    ]
    before = copy.deepcopy(nodes)
    cloud = _cloud(area, {n["id"]: n for n in nodes}, links)
    assert cloud["shown_material"] == cloud["total_material"] == 2
    assert len(cloud["edges"]) == 1
    assert next(n for n in cloud["nodes"] if n["ref"]["source_id"] == "left")["passage_count"] == 2
    assert all(n["qualification"]["limitations"] == ["Partial source"] for n in cloud["nodes"])
    assert nodes == before


def test_cloud_sampling_does_not_deprioritize_json_or_review_like_labels():
    def member(identity, label):
        return {
            "id": identity,
            "kind": "record",
            "label": label,
            "ref": {"record_id": identity},
            "area_ids": ["area:a"],
            "reasons": [],
            "qualification": {"state": "accepted"},
        }

    nodes = [member(f"record:{index:02d}", f"middle-{index:02d}.md") for index in range(48)]
    nodes.append(member("record:json", "a-review.json"))
    area = {"id": "area:a", "member_ids": [node["id"] for node in nodes]}

    cloud = _cloud(area, {node["id"]: node for node in nodes}, [])

    assert cloud["shown_material"] == 48
    assert "record:json" in {node["id"] for node in cloud["nodes"]}


def test_navigation_features_drop_serialization_noise_and_keep_natural_language():
    tokens = _tokens(
        '"id": "01ABCDEFGHIJK123", "active": true, "value": null, "topic": "watercolor pigment" https://www.example.com/papers/file.json [[record-id|Painting]]'
    )
    assert {"watercolor", "pigment", "painting"} <= set(tokens)
    assert not {"true", "null", "example", "json", "active", "record_id"} & set(tokens)
    assert display_title("inbox/2026-09-11-thinking-notes.md") == "Thinking notes"
    assert display_title("Owner — Learning by making") == "Learning by making"


def test_landscape_pages_are_bounded_and_invalid_input_rejected(tmp_path, monkeypatch):
    vault, _ = _vault(tmp_path)
    snap = Organization(Gateway(vault)).snapshot()
    area = snap["areas"][0]
    snap["areas"] = [dict(area, id=f"area:{i}", label=f"Theme {i}") for i in range(14)]
    monkeypatch.setattr(Organization, "_snapshot_view", lambda *a, **kw: snap)
    first = build_v2_landscape(vault)
    second = build_v2_landscape(vault, offset=first["page"]["next_offset"])
    assert len(first["nodes"]) == len(second["nodes"]) == 6
    assert not {a["id"] for a in first["nodes"]} & {a["id"] for a in second["nodes"]}
    assert first["page"]["total"] == 14
    with pytest.raises(V2Error):
        build_v2_landscape(vault, limit=7)
    with pytest.raises(V2Error):
        build_v2_landscape(vault, offset=-1)


def test_live_landscape_route_uses_same_pinned_read_contract(tmp_path):
    vault, store = _vault(tmp_path)
    handler = type("Handler", (web.SynapseHandler,), {"vault": vault})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/v2/landscape") as response:
            payload = json.load(response)
        assert payload["knowledge_revision"] == store.head()
        assert payload["clouds"] and payload["directory"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
