from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.gateway import Gateway
from synapse.knowledge import encode_record, record_descriptor
from synapse.organization import Organization
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes


def _record(identity: str, name: str, body: str) -> tuple[dict, bytes]:
    raw = (
        f"---\nid: {identity}\ntype: insight\nname: {name}\n"
        "review_status: proposed\nproperties: {}\n---\n\n"
        f"{body}\n"
    ).encode()
    return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw


def _v2_record(identity: str, *, name: str, availability: str, disposition: str) -> tuple[dict, bytes]:
    payload = json.loads(
        (Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json").read_text()
    )["payload"]
    payload.update(
        id=identity,
        subject_id="me",
        claim_key=name.casefold(),
        record_kind="question",
        availability=availability,
        evidence=[],
        context_refs=[],
        statement=f"{name} should never become a material area.",
    )
    payload["owner_review"] = {
        "status": "reviewed",
        "disposition": disposition,
        "receipt_id": generate_ulid(),
    }
    raw = encode_record(payload, name=name)
    return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw


def _vault(tmp_path: Path, *, with_source: bool = True) -> tuple[Path, RevisionStore]:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    rows = [
        _record("r1", "Latency", "distributed systems latency budget and queueing behavior"),
        _record("r2", "Queues", "distributed systems queueing behavior and latency budget"),
        _record("r3", "Capacity", "distributed systems capacity planning and queueing behavior"),
        _record("unmatched", "Gardening", "tomatoes compost seedlings and spring watering"),
    ]
    objects = {row[0]["version"]: row[1] for row in rows}
    records = {row[0]["id"]: row[0] for row in rows}
    sources: dict[str, str] = {}
    versions: dict[str, dict] = {}
    if with_source:
        descriptor, source_objects = prepare_source(
            b"Distributed systems latency and queueing.\n\nTomatoes compost seedlings.",
            origin="fixture.md",
            source_id=generate_ulid(),
            source_family_id=generate_ulid(),
            captured_at="2026-09-11T08:00:00Z",
        )
        objects.update(source_objects)
        sources[descriptor["id"]] = descriptor["version"]
        versions[descriptor["version"]] = descriptor
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"organization-fixture"),
        objects=objects,
        initialize=True,
        mutate=lambda manifest, _read: (
            manifest["records"].update(records),
            manifest["sources"].update(sources),
            manifest["source_versions"].update(versions),
        ),
    )
    return vault, store


def test_snapshot_has_typed_source_members_coverage_and_stable_rebuild(tmp_path: Path) -> None:
    vault, store = _vault(tmp_path)
    organization = Organization(Gateway(vault))
    first = organization.snapshot(rebuild=True)
    second = organization.snapshot(rebuild=True)

    assert first == second
    assert first["organization_revision"] == second["organization_revision"]
    assert first["method"] == "lexical-tfidf"
    member_ids = {item["id"] for item in first["members"]}
    assert first["member_links"]
    assert all(link["from"] in member_ids and link["to"] in member_ids for link in first["member_links"])
    similarity_pairs = [
        tuple(sorted((link["from"], link["to"])))
        for link in first["member_links"]
        if link["channel"] == "similarity"
    ]
    assert len(similarity_pairs) == len(set(similarity_pairs))
    source_members = [item for item in first["members"] if item["kind"] == "source"]
    assert source_members
    assert source_members[0]["ref"]["evidence"][0]["source_version"] in store.manifest()["source_versions"]
    assert all("availability" in item["qualification"] for item in first["members"] if item["kind"] == "record")
    source_qualification = source_members[0]["qualification"]
    assert source_qualification["basis"] == "retained-source-passage"
    assert source_qualification["assertion"] is False
    assert source_qualification["extraction_state"] == "complete"
    assert source_qualification["processing_state"] == "captured"
    assert "unique_records" in first["coverage"]
    assert "unique_sources" in first["coverage"]


def test_member_links_preserve_legacy_direction_and_identity_shape(tmp_path: Path) -> None:
    vault, _store = _vault(tmp_path, with_source=False)
    organization = Organization(Gateway(vault))
    members = [
        {"id": "record:left", "kind": "record", "record_id": "left"},
        {"id": "record:right", "kind": "record", "record_id": "right"},
    ]
    links = organization._member_links(
        [{"id": "area:test", "member_ids": ["record:left", "record:right"]}],
        members,
        [
            {
                "id": "legacy:edge-1",
                "record_id": None,
                "from_id": "left",
                "to_id": "right",
                "relation": "knows",
            },
            {
                "id": "legacy:edge-2",
                "record_id": None,
                "from_id": "right",
                "to_id": "left",
                "relation": "supports",
            },
        ],
        {0: [(1, 0.5, "lexical")], 1: [(0, 0.5, "lexical")]},
    )
    recorded = [link for link in links if link["channel"] == "recorded"]
    assert {(link["from"], link["to"]) for link in recorded} == {
        ("record:left", "record:right"),
        ("record:right", "record:left"),
    }
    assert {link["assertion_id"] for link in recorded} == {None}
    assert {link["legacy_edge_id"] for link in recorded} == {"legacy:edge-1", "legacy:edge-2"}


def test_areas_page_and_area_preserve_qualified_member_references(tmp_path: Path) -> None:
    vault, _store = _vault(tmp_path)
    organization = Organization(Gateway(vault))
    snapshot = organization.snapshot(rebuild=True)
    areas = organization.areas(limit=100)
    assert areas["organization_revision"] == snapshot["organization_revision"]
    assert areas["items"]
    selected = areas["items"][0]
    detail = organization.area(selected["id"], organization_revision=snapshot["organization_revision"], limit=150)
    assert detail["area"]["id"] == selected["id"]
    assert all("qualification" in member for member in detail["items"])
    assert all(member["kind"] in {"record", "source"} for member in detail["items"])


def test_suppressed_record_does_not_influence_labels_or_features(tmp_path: Path) -> None:
    vault, store = _vault(tmp_path, with_source=False)
    draft, draft_raw = _v2_record(
        generate_ulid(), name="Secret Astronaut Draft", availability="draft", disposition="none"
    )
    dismissed, dismissed_raw = _v2_record(
        generate_ulid(), name="Secret Astronaut Dismissed", availability="accepted", disposition="dismissed"
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"suppressed"),
        objects={draft["version"]: draft_raw, dismissed["version"]: dismissed_raw},
        mutate=lambda manifest, _read: manifest["records"].update(
            {draft["id"]: draft, dismissed["id"]: dismissed}
        ),
    )
    value = Organization(Gateway(vault)).snapshot(rebuild=True)
    assert all("astronaut" not in area["label"].casefold() for area in value["areas"])
    member_ids = {item["id"] for item in value["members"]}
    assert f"record:{draft['id']}" not in member_ids
    assert f"record:{dismissed['id']}" not in member_ids
    assert value["coverage"]["suppressed_records"] >= 2


def test_exact_duplicate_v2_records_are_deduplicated_before_features(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    first, first_raw = _v2_record(
        generate_ulid(), name="Duplicate-only", availability="accepted", disposition="none"
    )
    second, second_raw = _v2_record(
        generate_ulid(), name="Duplicate-only", availability="accepted", disposition="none"
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"duplicate-only"),
        objects={first["version"]: first_raw, second["version"]: second_raw},
        initialize=True,
        mutate=lambda manifest, _read: manifest["records"].update(
            {first["id"]: first, second["id"]: second}
        ),
    )
    value = Organization(Gateway(vault)).snapshot(rebuild=True)
    assert value["areas"] == []
    assert value["coverage"]["input_records"] == 1
    assert value["coverage"]["duplicate_records"] == 1
    duplicate_ref = value["coverage"]["duplicate_record_refs"][0]
    assert duplicate_ref["representative_id"] == min(first["id"], second["id"])
    assert duplicate_ref["duplicate_id"] == max(first["id"], second["id"])


def test_copied_source_is_readable_but_cannot_add_independent_support(tmp_path: Path) -> None:
    vault, store = _vault(tmp_path)
    descriptor, objects = prepare_source(
        b"Distributed systems latency and queueing.\n\nTomatoes compost seedlings.",
        origin="copied-fixture.md",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"copied-source"),
        objects=objects,
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
    )
    value = Organization(Gateway(vault)).snapshot(rebuild=True)
    source_members = [item for item in value["members"] if item["kind"] == "source"]
    assert len({item["ref"]["source_id"] for item in source_members}) == 1
    assert value["coverage"]["input_sources"] == 2
    assert value["coverage"]["duplicate_sources"] == 1
    assert value["coverage"]["duplicate_source_passages"] >= 1


def test_dominant_import_keeps_a_small_coherent_area_reachable(tmp_path: Path) -> None:
    vault, store = _vault(tmp_path, with_source=False)
    extra, extra_raw = _record(
        "small3", "Throughput", "distributed systems latency budget and queueing behavior"
    )
    dominant, dominant_objects = prepare_source(
        "\n\n".join(f"distributed systems queueing import line {index}" for index in range(300)).encode(),
        origin="dominant-import.txt",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"dominant-import"),
        objects={extra["version"]: extra_raw, **dominant_objects},
        mutate=lambda manifest, _read: (
            manifest["records"].update({extra["id"]: extra}),
            manifest["sources"].update({dominant["id"]: dominant["version"]}),
            manifest["source_versions"].update({dominant["version"]: dominant}),
        ),
    )
    value = Organization(Gateway(vault)).snapshot(rebuild=True)
    assert any({"record:r1", "record:r2", "record:small3"} <= set(area["member_ids"]) for area in value["areas"])


def test_missing_projection_is_structured_and_lexical_limit_is_honest(tmp_path: Path) -> None:
    vault, _store = _vault(tmp_path)
    organization = Organization(Gateway(vault))
    value = organization.snapshot(rebuild=True)
    path = vault / ".synapse" / "organization" / f"{value['organization_revision']}.json"
    path.unlink()
    with pytest.raises(V2Error) as caught:
        organization.snapshot(organization_revision=value["organization_revision"])
    assert caught.value.code == "revision-unavailable"
    assert "paraphrase" in " ".join(value["limitations"])


def test_projection_and_cache_integrity_are_defensive(tmp_path: Path) -> None:
    vault, _store = _vault(tmp_path)
    organization = Organization(Gateway(vault))
    value = organization.snapshot(rebuild=True)
    borrowed = organization._snapshot_view(organization_revision=value["organization_revision"])
    assert borrowed is Organization(Gateway(vault))._snapshot_view(organization_revision=value["organization_revision"])
    with pytest.raises(TypeError):
        borrowed["areas"].append({})
    public = organization.snapshot(organization_revision=value["organization_revision"])
    public["areas"].clear()
    assert borrowed["areas"]
    with pytest.raises(V2Error) as invalid:
        organization.snapshot(organization_revision="A" * 64)
    assert invalid.value.code == "invalid-request"

    path = vault / ".synapse" / "organization" / f"{value['organization_revision']}.json"
    tampered = json.loads(path.read_text())
    tampered["coverage"]["input_records"] += 1
    path.write_text(json.dumps(tampered, sort_keys=True))
    with pytest.raises(V2Error) as corrupt:
        Organization(Gateway(vault)).snapshot(organization_revision=value["organization_revision"])
    assert corrupt.value.code == "revision-unavailable"

    cache_key = "a" * 64
    organization._write_cache("test-cache", cache_key, {"key": cache_key, "value": 1})
    cache_path = vault / ".synapse" / "organization" / "test-cache" / f"{cache_key}.json"
    cached = json.loads(cache_path.read_text())
    cached["value"] = 2
    cache_path.write_text(json.dumps(cached, sort_keys=True))
    assert organization._read_cache("test-cache", cache_key) is None


def test_changed_inputs_reuse_content_features_and_change_projection_identity(tmp_path: Path) -> None:
    vault, store = _vault(tmp_path, with_source=False)
    first_organization = Organization(Gateway(vault))
    first = first_organization.snapshot(rebuild=True)
    feature_directory = vault / ".synapse" / "organization" / "features"
    before = {path.name: path.read_bytes() for path in feature_directory.glob("*.json")}
    added, added_raw = _record("changed", "Changed Input", "distinct retained material for a later revision")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"changed-input"),
        objects={added["version"]: added_raw},
        mutate=lambda manifest, _read: manifest["records"].update({added["id"]: added}),
    )
    second = Organization(Gateway(vault)).snapshot(rebuild=True)
    assert second["organization_revision"] != first["organization_revision"]
    after = {path.name: path.read_bytes() for path in feature_directory.glob("*.json")}
    assert before.items() <= after.items()


def test_lineage_reports_deterministic_split_and_merge_events(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "vault")
    _publish_topics(store, combined=False, initialize=True)
    base = Organization(Gateway(store.vault)).snapshot(rebuild=True)
    assert len(base["areas"]) == 2

    _publish_topics(store, combined=True)
    merged = Organization(Gateway(store.vault)).snapshot(rebuild=True)
    assert len(merged["areas"]) == 1
    assert merged["lineage"] == [{
        "kind": "merge",
        "area_id": merged["areas"][0]["id"],
        "previous_area_ids": sorted(area["id"] for area in base["areas"]),
    }]

    _publish_topics(store, combined=False)
    split = Organization(Gateway(store.vault)).snapshot(rebuild=True)
    assert len(split["areas"]) == 2
    assert split["lineage"] == [{
        "kind": "split",
        "previous_area_id": merged["areas"][0]["id"],
        "area_ids": sorted(area["id"] for area in split["areas"]),
    }]
    assert Organization(Gateway(store.vault)).snapshot(rebuild=True) == split


def _publish_topics(store: RevisionStore, *, combined: bool, initialize: bool = False) -> None:
    topics = ("harbor sailing tides ropes mooring winds", "furnace clay glazing pottery kiln firing")
    rows = [
        _record(f"topic-{index}", f"Note {index}", " ".join(topics) if combined else topics[index // 3])
        for index in range(6)
    ]
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(f"topics:{combined}".encode()),
        objects={row["version"]: raw for row, raw in rows},
        initialize=initialize,
        mutate=lambda manifest, _read: manifest["records"].update({row["id"]: row for row, _raw in rows}),
    )
