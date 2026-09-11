from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from synapse.knowledge import encode_record, record_descriptor
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error

ROOT = Path(__file__).parents[1]
EXAMPLE = json.loads(
    (ROOT / "docs/v2/contracts/example-knowledge_record.json").read_text(encoding="utf-8")
)["payload"]


def _payload(identity: str, *, statement: str = "A retained claim.", **fields: object) -> dict:
    value = copy.deepcopy(EXAMPLE)
    value.update(id=identity, availability="accepted", statement=statement)
    value.update(fields)
    return value


def _record(identity: str, *, path: str | None = None, **fields: object) -> tuple[dict, bytes]:
    raw = encode_record(_payload(identity, **fields), name="Same name")
    path = path or f"entities/insights/{identity}.md"
    return record_descriptor(raw, path=path), raw


def _legacy(identity: str, *, name: str = "Legacy", properties: dict | None = None, relations: list | None = None, aliases: list[str] | None = None, body: str = "Legacy body — café 😀\n", path: str | None = None, entity_type: str = "person") -> tuple[dict, bytes]:
    metadata = {"id": identity, "type": entity_type, "name": name, "review_status": "proposed"}
    if aliases is not None:
        metadata["aliases"] = aliases
    if properties is not None:
        metadata["properties"] = properties
    if relations is not None:
        metadata["relations"] = relations
    raw = ("---\n" + "".join(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value}\n" for key, value in metadata.items()) + "---\n\n" + body).encode("utf-8")
    # JSON-in-YAML is valid YAML and keeps this helper independent from the
    # editable checkout.
    return record_descriptor(raw, path=path or f"entities/people/{identity}.md"), raw


def _commit(vault: Path, rows: list[tuple[dict, bytes]], *, sources: list[tuple[dict, dict[str, bytes]]] | None = None, legacy_edges: list[dict] | None = None) -> RevisionStore:
    store = RevisionStore(vault)
    objects: dict[str, bytes] = {}
    descriptors = {}
    for row, raw in rows:
        descriptors[row["id"]] = row
        objects[row["version"]] = raw
    source_descriptors = {}
    source_pointers = {}
    for descriptor, source_objects in sources or []:
        source_descriptors[descriptor["version"]] = descriptor
        source_pointers[descriptor["id"]] = descriptor["version"]
        objects.update(source_objects)

    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hashlib.sha256(json.dumps(descriptors, sort_keys=True).encode()).hexdigest(),
        objects=objects,
        initialize=True,
        mutate=lambda manifest, _read: (
            manifest["records"].update(descriptors),
            manifest["source_versions"].update(source_descriptors),
            manifest["sources"].update(source_pointers),
            manifest.update(legacy_edges=legacy_edges or []),
        ),
    )
    return store


def _error(callable_, code: str) -> None:
    with pytest.raises(V2Error) as caught:
        callable_()
    assert caught.value.code == code


def test_reads_are_pinned_to_manifest_and_rebuild_after_index_deletion(tmp_path: Path) -> None:
    identity = generate_ulid()
    row, raw = _record(identity, statement="old retained statement")
    store = _commit(tmp_path / "vault", [(row, raw)])
    view = ReadView(store.vault)
    old_revision = view.revision
    assert view.records(ids=[identity])[0]["statement"] == "old retained statement"
    assert view.index_path == store.vault / ".synapse" / "v2-indexes" / f"{old_revision}.db"

    changed_row, changed_raw = _record(identity, statement="new HEAD statement")
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="suggestion-admission",
        payload_hash=hashlib.sha256(b"new").hexdigest(), objects={changed_row["version"]: changed_raw},
        mutate=lambda manifest, _read: manifest["records"].update({identity: changed_row}),
    )
    assert view.records(ids=[identity])[0]["statement"] == "old retained statement"
    view.index_path.unlink()
    rebuilt = ReadView(store.vault, revision=old_revision)
    assert rebuilt.records(ids=[identity])[0]["statement"] == "old retained statement"


def test_scope_happens_before_paging_and_property_only_owner_anchor_is_searchable(tmp_path: Path) -> None:
    rows = []
    for index in range(510):
        identity = generate_ulid()
        rows.append(_record(identity, statement=f"scoped claim {index}"))
    legacy_row, legacy_raw = _legacy(
        "me", name="Owner", properties={"knowledge_profile": "owner-knowledge-v1", "subject_id": "me", "claim_key": "anchor", "facets": ["planning"]}
    )
    store = _commit(tmp_path / "vault", rows + [(legacy_row, legacy_raw)])
    view = ReadView(store.vault)
    result = view.catalog(subject_id="me", facet="planning", limit=20)
    assert result["total"] == 1
    assert result["items"][0]["id"] == "me"
    assert view.catalog(query="anchor", subject_id="me")["total"] == 1


def test_valid_at_is_inclusive_and_does_not_use_as_of_as_cutoff(tmp_path: Path) -> None:
    identity = generate_ulid()
    row, raw = _record(identity, as_of="2026-09-10", applies_from="2020-01-01", applies_until="2026-09-10")
    store = _commit(tmp_path / "vault", [(row, raw)])
    view = ReadView(store.vault)
    assert view.candidates(valid_at="2026-09-10")["total"] == 1
    assert view.candidates(valid_at="2026-09-11")["total"] == 0


def test_explicit_ids_respect_applicability_and_legacy_history_stays_available(tmp_path):
    from synapse.gateway import Gateway
    identity = generate_ulid()
    claim = _record(identity, applies_from="2020-01-01", applies_until="2026-09-10")
    legacy = _legacy("me", properties={"knowledge_profile": "owner-knowledge-v1", "lifecycle": "historical"}, relations=[{"type": "related_to", "target": identity}])
    vault = tmp_path / "vault"
    _commit(vault, [claim, legacy])
    gateway = Gateway(vault)
    expired = gateway.context(ids=[identity], valid_at="2026-09-11")["items"][0]
    assert expired["withheld"] and expired["records"] == []
    assert gateway.context(ids=[identity], valid_at="2026-09-10")["items"][0]["records"]
    assert gateway.view.candidates(query="Legacy")["total"] == 1
    assert len(gateway.view.relationships(include_suggestions=False)) == 1


def test_candidates_use_bm25_before_late_name_tiebreak(tmp_path: Path) -> None:
    first_id, second_id = generate_ulid(), generate_ulid()
    first_payload = _payload(first_id, statement="needle")
    second_payload = _payload(second_id, statement="needle needle needle")
    first_raw = encode_record(first_payload, name="A later-name decoy")
    second_raw = encode_record(second_payload, name="Z more relevant")
    first = record_descriptor(first_raw, path=f"entities/insights/{first_id}.md")
    second = record_descriptor(second_raw, path=f"entities/insights/{second_id}.md")
    store = _commit(tmp_path / "vault", [(first, first_raw), (second, second_raw)])
    result = ReadView(store.vault).candidates(query="needle", limit=2)
    assert [item["id"] for item in result["items"]] == [second_id, first_id]


def test_source_catalog_rejects_record_only_filters(tmp_path: Path) -> None:
    row, raw = _record(generate_ulid())
    store = _commit(tmp_path / "vault", [(row, raw)])
    view = ReadView(store.vault)
    for kwargs in ({"subject_id": "me"}, {"facet": "planning"}, {"availability": "accepted"}):
        _error(lambda kwargs=kwargs: view.catalog(kind="sources", **kwargs), "invalid-request")


def test_known_at_uses_committed_history_and_rejects_pre_baseline(tmp_path: Path) -> None:
    identity = generate_ulid()
    row, raw = _record(identity)
    store = _commit(tmp_path / "vault", [(row, raw)])
    baseline = store.manifest()["committed_at"]
    assert ReadView(store.vault, known_at=baseline).revision == store.head()
    _error(lambda: ReadView(store.vault, known_at="2000-01-01T00:00:00Z"), "unsupported-history")
    _error(lambda: ReadView(store.vault, revision=store.head(), known_at=baseline), "invalid-request")
    _error(lambda: ReadView(store.vault, timezone="Not/A_Timezone"), "invalid-request")


def test_source_and_markdown_pages_preserve_late_unicode_and_source_version_pin(tmp_path: Path) -> None:
    row, raw = _record(generate_ulid(), statement="record — 😀")
    descriptor, objects = prepare_source("prefix\nlate evidence — café 😀\nsuffix".encode(), origin="fixture", source_id=generate_ulid(), captured_at="2026-09-10T08:00:00Z")
    store = _commit(tmp_path / "vault", [(row, raw)], sources=[(descriptor, objects)])
    view = ReadView(store.vault)
    page = view.source(descriptor["id"], limit=4000)
    assert "late evidence — café 😀" in page["text"]
    markdown = view.markdown(row["id"], limit=10)
    assert markdown["markdown"] == raw.decode("utf-8")[:10]
    assert view.markdown(row["id"], offset=markdown["next_offset"], limit=4000)["markdown"]
    _error(lambda: view.source(descriptor["id"], version=hashlib.sha256(b"wrong").hexdigest()), "source-unavailable")


def test_incoming_freezes_referrer_shape_and_imports_profiled_legacy_corrections(tmp_path: Path) -> None:
    target_id = generate_ulid()
    correction_id = generate_ulid()
    target, target_raw = _record(target_id)
    correction, correction_raw = _record(
        correction_id,
        dependencies=[{"id": target_id, "version": target["version"], "role": "premise"}],
        context_refs=[{"id": target_id, "version": target["version"], "role": "qualifies", "scope": "the time window"}],
    )
    legacy, legacy_raw = _legacy(
        generate_ulid(),
        properties={"knowledge_profile": "owner-knowledge-v1", "subject_id": "me", "claim_key": "legacy"},
        relations=[{"type": "related_to", "target": target_id, "properties": {"roles": ["revises"], "scope": "legacy correction"}}],
    )
    store = _commit(tmp_path / "vault", [(target, target_raw), (correction, correction_raw), (legacy, legacy_raw)])
    refs = ReadView(store.vault).incoming(target_id)
    assert {tuple(sorted(item)) for item in refs} == {
        ("id", "role", "scope", "target_id", "target_version", "version"),
        ("id", "role", "target_id", "target_version", "version"),
    }
    assert {item["role"] for item in refs} == {"premise", "qualifies", "revises"}
    assert all(item["target_id"] == target_id for item in refs)


def test_relationships_keep_parallel_v2_assertions_and_exclude_tombstones(tmp_path: Path) -> None:
    left, left_raw = _record(generate_ulid(), path="entities/people/left.md")
    right, right_raw = _record(generate_ulid(), path="entities/people/right.md")
    relationships = []
    for scope in ("first episode", "second episode"):
        assertion_id = generate_ulid()
        relationships.append(_record(assertion_id, relationship={"from_id": left["id"], "to_id": right["id"], "relation_type": "knows", "scope": scope}))
    store = _commit(tmp_path / "vault", [(left, left_raw), (right, right_raw), *relationships])
    result = ReadView(store.vault).relationships(identity=left["id"])
    assert [item["scope"] for item in result] == ["first episode", "second episode"]
    assert len({item["id"] for item in result}) == 2


def test_relationships_omit_dismissed_disputed_and_draft_suggestions(tmp_path: Path) -> None:
    left, left_raw = _record(generate_ulid(), path="entities/people/left.md")
    right, right_raw = _record(generate_ulid(), path="entities/people/right.md")
    rows = [(left, left_raw), (right, right_raw)]
    expected = None
    for disposition in ("none", "dismissed", "disputed"):
        assertion_id = generate_ulid()
        review = {"status": "not-reviewed", "disposition": "none"}
        if disposition != "none":
            review = {"status": "reviewed", "disposition": disposition, "receipt_id": generate_ulid()}
        assertion, assertion_raw = _record(
            assertion_id,
            availability="suggestion",
            owner_review=review,
            relationship={"from_id": left["id"], "to_id": right["id"], "relation_type": "knows", "scope": disposition},
        )
        rows.append((assertion, assertion_raw))
        if disposition == "none":
            expected = assertion_id
    draft_id = generate_ulid()
    draft, draft_raw = _record(
        draft_id,
        availability="draft",
        relationship={"from_id": left["id"], "to_id": right["id"], "relation_type": "knows", "scope": "draft"},
    )
    rows.append((draft, draft_raw))
    store = _commit(tmp_path / "vault", rows)
    result = ReadView(store.vault).relationships(identity=left["id"], include_suggestions=True)
    assert [item["id"] for item in result] == [expected]


def test_legacy_relations_match_core_resolver_and_ignore_stale_manifest_edges(tmp_path: Path) -> None:
    source_id = generate_ulid()
    target_id = generate_ulid()
    source, source_raw = _legacy(
        source_id,
        name="Declaring",
        aliases=["Unique Source Alias"],
        relations=[{"type": "knows", "target": "Unique Target Alias", "properties": {"kind": "typed"}, "source": "legacy.csv"}],
        body="A weak mention [[target-slug]]\n",
    )
    target, target_raw = _legacy(
        target_id,
        name="Canonical Target",
        aliases=["Unique Target Alias"],
        path="entities/people/target-slug.md",
    )
    store = _commit(
        tmp_path / "vault",
        [(source, source_raw), (target, target_raw)],
        legacy_edges=[{"id": "stale-edge", "from_id": source_id, "to_id": target_id, "type": "withdrawn_relation"}],
    )
    view = ReadView(store.vault)
    result = view.relationships()
    assert {item["relation"] for item in result} == {"knows", "mentioned_in"}
    assert all(item["id"] != "stale-edge" for item in result)
    assert next(item for item in result if item["relation"] == "knows")["properties"] == {"kind": "typed"}

    from synapse.index import _relations_for_entities
    from synapse.parser import _parse_entity_file_internal

    entities = []
    for row, raw in ((source, source_raw), (target, target_raw)):
        entity, issues = _parse_entity_file_internal(store.vault / row["path"], store.vault, retained_bytes=raw)
        assert entity is not None and not issues
        entities.append(entity)
    core, issues = _relations_for_entities(entities)
    assert not issues
    assert [(item["from_id"], item["to_id"], item["relation"], item["weak"], item["properties"], item["source_file"], item["review_status"]) for item in result] == [
        (item.from_id, item.to_id, item.type, item.weak, item.properties, item.source_file, item.review_status) for item in core
    ]

    withdrawn_source, withdrawn_raw = _legacy(
        source_id,
        name="Declaring",
        aliases=["Unique Source Alias"],
        body="The canonical revision withdrew its relation.\n",
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hashlib.sha256(b"withdraw relation").hexdigest(),
        objects={withdrawn_source["version"]: withdrawn_raw},
        mutate=lambda manifest, _read: manifest["records"].update({source_id: withdrawn_source}),
    )
    assert ReadView(store.vault).relationships() == []
