"""Adaptive navigation must preserve coverage, identity and evidence boundaries."""

from __future__ import annotations

import copy
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest
import yaml
from test_v2_gateway import corpus as qualified_corpus  # noqa: F401
from test_v2_organization import _vault

from synapse.gateway import Gateway, transport_size
from synapse.knowledge import record_descriptor
from synapse.organization import Organization
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.web import SynapseHandler
from synapse.web_explore import SCENE_BUDGET, build_v2_browse, build_v2_threads


def adaptive_vault(path, count=340):
    vault = path / "brain"
    store = RevisionStore(vault)
    rows = []

    def add(identity, name, kind, relations=(), body=""):
        metadata = {
            "id": identity,
            "type": kind,
            "name": name,
            "review_status": "proposed",
            "properties": {},
            "relations": list(relations),
        }
        raw = ("---\n" + yaml.safe_dump(metadata) + "---\n\n" + body).encode()
        rows.append((record_descriptor(raw, path=f"entities/{kind}/{identity}.md"), raw))

    add("studio", "Watercolor Studio", "company")
    add("workshop", "Ceramic Workshop", "company")
    for i in range(count):
        target = "studio" if i % 2 == 0 else "workshop"
        add(f"p{i:03}", f"Maker {i:03}", "person", [{"type": "works_at", "target": target}])
    for i in range(55):
        add(
            f"c{i:03}",
            f"Conversation with Maker {i % 4:03} · {i}",
            "conversation",
            body="Saved conversation: café 水彩.\n" * 200,
        )
    add(
        "host",
        "Studio Host",
        "person",
        [{"type": "participated_in", "target": f"c{i:03}"} for i in range(55)],
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"adaptive fixture"),
        objects={d["version"]: raw for d, raw in rows},
        initialize=True,
        mutate=lambda m, _: m["records"].update({d["id"]: d for d, _ in rows}),
    )
    return vault, store


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    vault, store = adaptive_vault(tmp_path)
    snapshot = Organization(Gateway(vault), config={"community_resolution": 0.2}).snapshot()
    # One synthetic collection containing eligible retained members. Only its
    # navigation boundary is controlled; records/relations use production reads.
    area = copy.deepcopy(snapshot["areas"][0])
    area.update(
        id="area:fixture", label="Studio network", member_ids=[m["id"] for m in snapshot["members"]]
    )
    snapshot["areas"] = [area]
    monkeypatch.setattr(Organization, "_snapshot_view", lambda *a, **kw: snapshot)
    return vault, store, snapshot


def test_small_area_has_complete_material_and_canonical_source_refs(tmp_path):
    vault, _ = _vault(tmp_path)
    snapshot = Organization(Gateway(vault)).snapshot()
    area = snapshot["areas"][0]
    result = build_v2_browse(
        vault, area["id"], organization_revision=snapshot["organization_revision"]
    )
    assert result["mode"] == "map"
    assert result["page"]["next_offset"] is None
    assert len(result["nodes"]) == result["total_material"]
    for node in result["nodes"]:
        original = next(m for m in snapshot["members"] if m["ref"] == node["ref"])
        assert node["qualification"] == original["qualification"]
    assert transport_size(result) <= SCENE_BUDGET


def test_large_collection_groups_by_retained_types_and_affiliations(corpus):
    vault, _, snap = corpus
    kwargs = {"organization_revision": snap["organization_revision"]}
    root = build_v2_browse(vault, "area:fixture", **kwargs)
    assert root["mode"] == "groups"
    assert sum(n["count"] for n in root["nodes"]) == root["total_material"]
    people = next(n for n in root["nodes"] if n["label"] == "People")
    sub = build_v2_browse(vault, "area:fixture", group_path=people["id"], **kwargs)
    studio = next(n for n in sub["nodes"] if n["label"] == "Watercolor Studio")
    assert studio["count"] == 170
    leaf = build_v2_browse(
        vault, "area:fixture", group_path=people["id"] + "/" + studio["id"], **kwargs
    )
    assert leaf["anchor"]["id"] == "studio"
    assert leaf["total_material"] == 170
    assert leaf["mode"] == "map"
    assert all(
        e["from"] in {n["id"] for n in leaf["nodes"]}
        and e["to"] in {n["id"] for n in leaf["nodes"]}
        for e in leaf["edges"]
    )
    assert any(e.get("metadata", {}).get("relation") == "works_at" for e in leaf["edges"])


def test_search_is_scoped_and_finds_recorded_affiliations(corpus):
    vault, _, snap = corpus
    kw = {"organization_revision": snap["organization_revision"]}
    root = build_v2_browse(vault, "area:fixture", **kw)
    people = next(n for n in root["nodes"] if n["label"] == "People")
    result = build_v2_browse(vault, "area:fixture", group_path=people["id"], query="Ceramic", **kw)
    assert result["total_material"] == 170
    assert all(n["type"] == "person" and int(n["id"][1:]) % 2 == 1 for n in result["nodes"])
    empty = build_v2_browse(vault, "area:fixture", query="does-not-exist", **kw)
    assert not empty["nodes"] and empty["page"]["next_offset"] is None


def test_threads_counts_before_cap_and_all_neighbors_remain_reachable(corpus):
    vault, _, snap = corpus
    kw = {"organization_revision": snap["organization_revision"]}
    root = build_v2_threads(vault, "studio", **kw)
    assert root["mode"] == "bundles" and root["total_neighbors"] == 170
    bundle = next(n for n in root["nodes"] if n["kind"] == "bundle")
    assert bundle["count"] == 170
    assert root["edges"] == []  # A container does not mint relationship evidence.
    seen, offset = set(), 0
    while offset is not None:
        page = build_v2_threads(vault, "studio", bundle=bundle["id"], offset=offset, **kw)
        assert page["nodes"][0]["id"] == "studio"
        ids = {n["id"] for n in page["nodes"][1:]}
        assert not seen & ids
        seen |= ids
        assert all("studio" in (e["from"], e["to"]) for e in page["edges"])
        assert all(e.get("qualification", {}).get("state") == "legacy-edge" for e in page["edges"])
        assert transport_size(page) <= SCENE_BUDGET
        offset = page["page"]["next_offset"]
    assert len(seen) == 170


def test_conversations_are_units_and_original_text_stays_available(corpus):
    vault, _, snap = corpus
    kw = {"organization_revision": snap["organization_revision"]}
    root = build_v2_threads(vault, "host", **kw)
    bundle = next(n for n in root["nodes"] if n["kind"] == "bundle")
    assert bundle["count"] == 55
    page = build_v2_threads(vault, "host", bundle=bundle["id"], **kw)
    assert all(n["type"] == "conversation" for n in page["nodes"][1:])
    assert "café 水彩" in Gateway(vault).record(page["nodes"][1]["id"], limit=4000)["text"]


def test_invalid_or_stale_navigation_is_explicit(corpus):
    vault, _, snap = corpus
    kw = {"organization_revision": snap["organization_revision"]}
    for args in ({"offset": -1}, {"group_path": "group:missing"}, {"query": "x" * 301}):
        with pytest.raises(V2Error):
            build_v2_browse(vault, "area:fixture", **kw, **args)
    with pytest.raises(V2Error):
        build_v2_threads(vault, "studio", bundle="missing", **kw)


def test_new_http_routes_use_pinned_reads_and_require_organization(corpus):
    vault, store, snap = corpus
    handler = type("AdaptiveHandler", (SynapseHandler,), {"vault": vault})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for route, args in (("browse", {"area_id": "area:fixture"}), ("threads", {"id": "host"})):
            args.update(organization_revision=snap["organization_revision"], revision=store.head())
            with urlopen(base + "/api/v2/" + route + "?" + urlencode(args)) as response:
                result = json.load(response)
            assert result["knowledge_revision"] == store.head()
            assert result["organization_revision"] == snap["organization_revision"]
        with pytest.raises(HTTPError):
            urlopen(base + "/api/v2/browse?area_id=area:fixture")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_adaptive_focus_preserves_qualifications_and_excludes_disputed(qualified_corpus):  # noqa: F811
    vault, _, add = qualified_corpus
    premise = add(statement="A possible watercolor learning preference.")
    subject = add(
        availability="accepted",
        statement="Watercolor practice may be useful.",
        dependencies=[
            {
                "id": premise["id"],
                "version": premise["version"],
                "role": "premise",
            }
        ],
    )
    disputed = add(statement="A disputed watercolor explanation.", disposition="disputed")
    snapshot = Organization(Gateway(vault)).snapshot()
    result = build_v2_threads(
        vault, subject["id"], organization_revision=snapshot["organization_revision"]
    )
    assert result["focus"]["qualification"].get("provisional_dependencies")
    assert premise["id"] in json.dumps(result["focus"]["qualification"])
    assert result["focus"]["ref"]["record_version"] == subject["version"]
    with pytest.raises(V2Error):
        build_v2_threads(
            vault, disputed["id"], organization_revision=snapshot["organization_revision"]
        )
