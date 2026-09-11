from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_v2_semantic import _legacy, _v2_record

from synapse.gateway import Gateway
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore
from synapse.source_purpose import load_source_purposes, write_source_purposes
from synapse.source_store import evidence_ref, prepare_source, read_passage
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes
from synapse.v2_semantic import SemanticRuntime, build_index, query


class PurposeEmbedder:
    """Explicit synthetic vectors; no model, network or agent runtime."""

    model = "synthetic-purpose-test"

    def embed(self, texts):
        return [
            [1.0, 0.0] if "internalmarker" in text else
            [0.8, 0.6] if "knowledgemarker" in text else
            [0.6, 0.8] if "unknownmarker" in text else [0.0, 1.0]
            for text in texts
        ]


@pytest.fixture
def corpus(tmp_path):
    sources = []
    for number, (origin, body) in enumerate([
        ("ordinary-notes.txt", "alpha internalmarker alpha alpha alpha"),
        ("build/smoke.txt", "alpha knowledgemarker preserved owner material"),
        ("evaluation/results.txt", "alpha unknownmarker unclassified material"),
    ], start=1):
        sources.append(prepare_source(
            body.encode(), origin=origin,
            source_id=f"01ARZ3NDEKTSV4RRFFQ69G5FA{number}",
            captured_at="2026-09-11T08:00:00Z",
        ))
    internal, knowledge, unknown = [pair[0] for pair in sources]
    owner = _legacy("me", "Owner", "alpha independently retained owner record")
    record_id = generate_ulid()
    owner_row, owner_raw = _v2_record(record_id, name="Owner assertion", evidence=[
        evidence_ref(knowledge, sources[1][1].__getitem__, 0, 5),
    ])
    suggestion_row, suggestion_raw = _v2_record(
        generate_ulid(), name="Useful qualification", availability="suggestion",
        evidence=[evidence_ref(internal, sources[0][1].__getitem__, 0, 5)],
        context_refs=[{"id": record_id, "version": owner_row["version"], "role": "qualifies", "scope": "Alternative explanation"}],
    )
    rows = [owner, (owner_row, owner_raw), (suggestion_row, suggestion_raw)]
    objects = {row["version"]: raw for row, raw in rows}
    for _, source_objects in sources:
        objects.update(source_objects)
    store = RevisionStore(tmp_path / "vault")
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"synthetic purpose corpus"), objects=objects,
        initialize=True,
        mutate=lambda manifest, _read: (
            manifest["records"].update({row["id"]: row for row, _ in rows}),
            manifest["sources"].update({row["id"]: row["version"] for row, _ in sources}),
            manifest["source_versions"].update({row["version"]: row for row, _ in sources}),
        ),
    )
    return store, (internal, knowledge, unknown), (owner_row, suggestion_row)


def _classification(source, purpose):
    return {"source_id": source["id"], "source_version": source["version"],
            "purpose": purpose, "reason": "Explicit synthetic review"}


def _classify(store, sources):
    return write_source_purposes(store.vault, revision=store.head(), classifications=[
        _classification(sources[0], "internal"), _classification(sources[1], "knowledge"),
    ])


def _policy_path(store, revision=None):
    return store.vault / ".synapse" / "source-purpose" / f"{revision or store.head()}.json"


def test_unknown_visible_without_filename_inference_and_no_policy_write(corpus):
    store, _, _ = corpus
    view = ReadView(store.vault)
    assert not _policy_path(store).exists()
    for response in (view.catalog(kind="sources"), view.search_sources("alpha")):
        assert response["total"] == 3
        assert {item["purpose"] for item in response["items"]} == {"unknown"}
        assert {item["purpose_basis"] for item in response["items"]} == {"unclassified"}
        assert response["source_policy"]["snapshot_hash"] is None
        assert response["semantic_search"] == "unused"
    assert not _policy_path(store).exists()


def test_filtering_precedes_counts_ranking_and_paging_and_retains_canonical_bytes(corpus):
    store, sources, _ = corpus
    retained = {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    view = ReadView(store.vault)
    policy = _classify(store, sources)
    assert retained == {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    for read in (lambda **kw: view.catalog(kind="sources", **kw), lambda **kw: view.search_sources("alpha", **kw)):
        first = read(limit=1)
        second = read(offset=first["next_offset"], limit=1)
        assert first["total"] == second["total"] == 2
        assert second["next_offset"] is None
        assert {item.get("source_id", item.get("id")) for response in (first, second) for item in response["items"]} == {row["id"] for row in sources[1:]}
        assert first["source_policy"] == policy.metadata()
        all_sources = read(source_scope="all")
        assert all_sources["total"] == 3
        assert {row["purpose"] for row in all_sources["items"]} == {"internal", "knowledge", "unknown"}
    assert view.catalog(kind="sources", query="ordinary-notes")["total"] == 0
    assert view.catalog(kind="sources", query="ordinary-notes", source_scope="all")["total"] == 1


def test_exact_reads_owner_records_suggestions_and_mandatory_closure_survive(corpus):
    store, sources, rows = corpus
    policy = _classify(store, sources)
    view = ReadView(store.vault)
    internal = sources[0]
    assert "internalmarker" in view.source(internal["id"])["text"]
    evidence = evidence_ref(internal, store.read_object, 0, 5)
    assert read_passage(internal, evidence, store.read_object)["excerpt"] == "alpha"
    assert {row["id"] for row in view.candidates(query="alpha")["items"]} == {"me", *(row["id"] for row in rows)}
    unit = Gateway(store.vault).context(ids=[rows[0]["id"]], budget_chars=32000)["items"][0]
    assert {row["id"] for row in unit["records"]} == {row["id"] for row in rows}
    assert unit["qualified"]
    assert policy.record_evidence_policy(view.records(ids=[rows[1]["id"]])[0]) == {
        "source_evidence_purpose": "internal-only", "source_purpose_exclusion_candidate": True,
    }
    assert not policy.record_evidence_policy(view.records(ids=[rows[0]["id"]])[0])["source_purpose_exclusion_candidate"]
    assert policy.record_evidence_policy(view.records(ids=["me"])[0])["source_evidence_purpose"] == "unlinked"
    mixed = {"evidence": [evidence, {"source_id": sources[1]["id"], "source_version": sources[1]["version"]}]}
    assert policy.record_evidence_policy(mixed) == {
        "source_evidence_purpose": "mixed", "source_purpose_exclusion_candidate": False,
    }


def test_semantic_filters_before_scoring_limit_without_rebuilding_warm_vectors(corpus):
    store, sources, _ = corpus
    embedder = PurposeEmbedder()
    report = build_index(store.vault, embedder=embedder)
    assert report["semantic"] == "ok"
    path = Path(report["path"])
    before = path.read_bytes()
    runtime = SemanticRuntime(store.vault, embedder)
    try:
        view = ReadView(store.vault)
        arguments = {"embedder": runtime, "include_sources": True, "limit": 1}
        unclassified = query(view, "internalmarker", **arguments)
        assert unclassified["candidates"][0]["source_id"] == sources[0]["id"]
        policy = _classify(store, sources)
        ordinary = query(view, "internalmarker", **arguments)
        assert ordinary["candidates"][0]["source_id"] == sources[1]["id"]
        assert ordinary["candidates"][0]["purpose"] == "knowledge"
        assert ordinary["source_policy"] == policy.metadata()
        inspection = query(view, "internalmarker", source_scope="all", **arguments)
        assert inspection["candidates"][0]["source_id"] == sources[0]["id"]
        assert inspection["candidates"][0]["purpose"] == "internal"
        cleared = write_source_purposes(store.vault, revision=view.revision, classifications=[_classification(sources[0], "unknown")])
        assert cleared.snapshot_hash != policy.snapshot_hash
        reopened = query(view, "internalmarker", **arguments)
        assert reopened["candidates"][0]["source_id"] == sources[0]["id"]
        assert reopened["source_policy"]["snapshot_hash"] == cleared.snapshot_hash
        assert path.read_bytes() == before
    finally:
        runtime.close()


def test_policy_inherits_only_exact_versions_and_rejects_misbound_snapshot(corpus):
    store, sources, _ = corpus
    old = store.head()
    _classify(store, sources)
    new_source, objects = prepare_source(b"alpha revised internal source", origin="ordinary-notes.txt", source_id=sources[0]["id"])
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"new source version"), objects=objects,
        mutate=lambda manifest, _read: (
            manifest["source_versions"].update({new_source["version"]: new_source}),
            manifest["sources"].update({new_source["id"]: new_source["version"]}),
        ),
    )
    assert ReadView(store.vault, revision=old).catalog(kind="sources")["total"] == 2
    assert ReadView(store.vault).catalog(kind="sources")["total"] == 3
    new_policy = write_source_purposes(store.vault, revision=store.head(), classifications=[_classification(sources[0], "internal")])
    assert new_policy.purpose(new_source["id"], new_source["version"]) == "unknown"
    assert ReadView(store.vault).search_sources("alpha")["total"] == 3
    _policy_path(store).write_bytes(_policy_path(store, old).read_bytes())
    with pytest.raises(V2Error, match="another revision"):
        ReadView(store.vault).search_sources("alpha")


@pytest.mark.parametrize("damage", ["checksum", "hash-binding", "duplicate", "truncated"])
def test_corrupt_policy_refuses_discovery_without_blocking_exact_reads(corpus, damage):
    store, sources, _ = corpus
    _classify(store, sources)
    path = _policy_path(store)
    envelope = json.loads(path.read_bytes())
    if damage == "checksum":
        envelope["payload"]["classifications"][0]["purpose"] = "knowledge"
    elif damage == "hash-binding":
        envelope["payload"]["classifications"][0]["original_hash"] = "0" * 64
        envelope["snapshot_hash"] = hash_bytes(canonical_json(envelope["payload"]))
    elif damage == "duplicate":
        envelope["payload"]["classifications"].append(envelope["payload"]["classifications"][0])
        envelope["snapshot_hash"] = hash_bytes(canonical_json(envelope["payload"]))
    path.write_bytes(b"{" if damage == "truncated" else canonical_json(envelope))
    view = ReadView(store.vault)
    for scope in ("ordinary", "all"):
        with pytest.raises(V2Error) as caught:
            view.search_sources("alpha", source_scope=scope)
        assert caught.value.code == "recovery-required"
    assert view.source(sources[0]["id"])["text"]
    assert view.records(ids=["me"])


@pytest.mark.parametrize("field,value", [("purpose", "smoke"), ("purpose", False), ("reason", ""), ("source_id", "unretained"), ("source_version", "0" * 64)])
def test_invalid_classifications_do_not_write_policy(corpus, field, value):
    store, sources, _ = corpus
    row = _classification(sources[0], "internal")
    row[field] = value
    with pytest.raises(V2Error) as caught:
        write_source_purposes(store.vault, revision=store.head(), classifications=[row])
    assert caught.value.code == "invalid-request"
    assert not _policy_path(store).exists()


@pytest.mark.parametrize("scope", [None, "hidden", [], True])
def test_scope_rejects_invalid_values_at_each_discovery_seam(corpus, scope):
    store, _, _ = corpus
    view = ReadView(store.vault)
    for read in (
        lambda: view.catalog(kind="sources", source_scope=scope),
        lambda: view.search_sources("alpha", source_scope=scope),
        lambda: query(view, "alpha", source_scope=scope),
    ):
        with pytest.raises(V2Error) as caught:
            read()
        assert caught.value.code == "invalid-request"


def test_concurrent_reviewed_updates_merge_without_lost_classifications(corpus):
    store, sources, _ = corpus
    def write(source):
        write_source_purposes(store.vault, revision=store.head(), classifications=[_classification(source, "knowledge")])
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(write, sources))
    policy = load_source_purposes(store.vault, revision=store.head())
    assert len(policy.classifications) == 3
    assert all(policy.purpose(row["id"], row["version"]) == "knowledge" for row in sources)


def test_noop_review_preserves_snapshot_digest_and_file(corpus):
    store, sources, _ = corpus
    empty = write_source_purposes(store.vault, revision=store.head(), classifications=[])
    assert empty.snapshot_hash is None
    assert not _policy_path(store).exists()
    policy = _classify(store, sources)
    before = _policy_path(store).stat()
    same = _classify(store, sources)
    after = _policy_path(store).stat()
    assert same.snapshot_hash == policy.snapshot_hash
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_duplicate_original_bytes_do_not_propagate_review(corpus):
    store, sources, _ = corpus
    duplicate, objects = prepare_source(
        store.read_object(sources[0]["original_hash"]), origin=sources[0]["origin"],
        source_id=generate_ulid(), source_family_id=sources[0]["source_family_id"],
    )
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"duplicate source"), objects=objects,
        mutate=lambda manifest, _read: (
            manifest["source_versions"].update({duplicate["version"]: duplicate}),
            manifest["sources"].update({duplicate["id"]: duplicate["version"]}),
        ),
    )
    policy = _classify(store, sources)
    assert duplicate["original_hash"] == sources[0]["original_hash"]
    assert policy.purpose(duplicate["id"], duplicate["version"]) == "unknown"
    hits = ReadView(store.vault).search_sources("internalmarker")
    assert hits["total"] == 1
    assert hits["items"][0]["source_id"] == duplicate["id"]


@pytest.mark.parametrize("revision", [None, "../elsewhere", False, ""])
def test_policy_helpers_require_explicit_revision(corpus, revision):
    store, _, _ = corpus
    for call in (
        lambda: load_source_purposes(store.vault, revision=revision),
        lambda: write_source_purposes(store.vault, revision=revision, classifications=[]),
    ):
        with pytest.raises(V2Error) as caught:
            call()
        assert caught.value.code == "invalid-request"


def _add_unrelated_record(store):
    row, raw = _legacy(generate_ulid(), "Independent addition", "A new retained record")
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(raw), objects={row["version"]: raw},
        mutate=lambda manifest, _read: manifest["records"].update({row["id"]: row}),
    )
    return store.head()


def test_unrelated_saves_inherit_exact_policy_without_writes_or_historical_change(corpus):
    store, sources, _ = corpus
    ancestor = store.head()
    original = _classify(store, sources)
    old_bytes = _policy_path(store, ancestor).read_bytes()
    intermediate = _add_unrelated_record(store)
    descendant = _add_unrelated_record(store)
    files_before = {str(path): path.read_bytes() for path in store.vault.rglob("*") if path.is_file()}
    inherited = load_source_purposes(store.vault, revision=descendant)
    assert inherited.purpose(sources[0]["id"], sources[0]["version"]) == "internal"
    assert inherited.purpose(sources[1]["id"], sources[1]["version"]) == "knowledge"
    assert inherited.purpose(sources[2]["id"], sources[2]["version"]) == "unknown"
    assert inherited.knowledge_revision == descendant
    assert inherited.inherited_from_revision == ancestor
    assert inherited.snapshot_hash != original.snapshot_hash
    assert inherited.metadata()["knowledge_revision"] == descendant
    assert inherited.metadata()["inherited_from_revision"] == ancestor
    assert inherited.snapshot_hash == load_source_purposes(store.vault, revision=descendant).snapshot_hash
    assert not _policy_path(store, intermediate).exists()
    assert not _policy_path(store, descendant).exists()
    assert files_before == {str(path): path.read_bytes() for path in store.vault.rglob("*") if path.is_file()}
    assert load_source_purposes(store.vault, revision=ancestor).metadata() == original.metadata()
    assert _policy_path(store, ancestor).read_bytes() == old_bytes
    assert ReadView(store.vault).search_sources("internalmarker")["total"] == 0
    expected_payload = json.loads(old_bytes)["payload"]
    expected_payload["knowledge_revision"] = descendant
    assert inherited.snapshot_hash == hash_bytes(canonical_json(expected_payload))


def test_descendant_unknown_override_survives_next_save_and_preserves_other_decisions(corpus):
    store, sources, _ = corpus
    ancestor = store.head()
    _classify(store, sources)
    old_bytes = _policy_path(store, ancestor).read_bytes()
    override_revision = _add_unrelated_record(store)
    override = write_source_purposes(store.vault, revision=override_revision, classifications=[
        _classification(sources[0], "unknown"),
    ])
    assert override.inherited_from_revision is None
    assert override.purpose(sources[1]["id"], sources[1]["version"]) == "knowledge"
    descendant = _add_unrelated_record(store)
    inherited = load_source_purposes(store.vault, revision=descendant)
    assert inherited.inherited_from_revision == override_revision
    assert inherited.purpose(sources[0]["id"], sources[0]["version"]) == "unknown"
    assert inherited.labels(sources[0]["id"], sources[0]["version"])["purpose_basis"] == "reviewed-local-policy"
    assert inherited.purpose(sources[1]["id"], sources[1]["version"]) == "knowledge"
    assert ReadView(store.vault).search_sources("internalmarker")["total"] == 1
    assert not _policy_path(store, descendant).exists()
    assert _policy_path(store, ancestor).read_bytes() == old_bytes


def test_corrupt_nearest_ancestor_refuses_without_fallback_to_older_valid_policy(corpus):
    store, sources, _ = corpus
    ancestor = store.head()
    original = _classify(store, sources)
    nearer = _add_unrelated_record(store)
    write_source_purposes(store.vault, revision=nearer, classifications=[_classification(sources[0], "unknown")])
    descendant = _add_unrelated_record(store)
    _policy_path(store, nearer).write_bytes(b"{")
    with pytest.raises(V2Error) as caught:
        load_source_purposes(store.vault, revision=descendant)
    assert caught.value.code == "recovery-required"
    assert not _policy_path(store, descendant).exists()
    assert load_source_purposes(store.vault, revision=ancestor).snapshot_hash == original.snapshot_hash


def test_inherited_noop_review_keeps_digest_when_materialized(corpus):
    store, sources, _ = corpus
    _classify(store, sources)
    descendant = _add_unrelated_record(store)
    inherited = load_source_purposes(store.vault, revision=descendant)
    written = write_source_purposes(store.vault, revision=descendant, classifications=[_classification(sources[0], "internal")])
    assert written.snapshot_hash == inherited.snapshot_hash
    assert written.inherited_from_revision is None
    assert _policy_path(store, descendant).is_file()


def test_internal_evidence_inspection_flags_survive_catalog_and_organization(tmp_path):
    from test_v2_read_view import _commit, _record

    from synapse.organization import Organization
    source, objects = prepare_source(b"Synthetic diagnostic outcome", origin="diagnostic.txt")
    row = _record(generate_ulid(), evidence=[evidence_ref(source, objects.__getitem__, 0, 9)],
                  dependencies=[], context_refs=[])
    store = _commit(tmp_path / "vault", [row], sources=[(source, objects)])
    gateway = Gateway(store.vault)
    organization = Organization(gateway)
    identity = row[0]["id"]
    organization.snapshot()
    write_source_purposes(store.vault, revision=store.head(), classifications=[_classification(source, "internal")])
    catalog = gateway.catalog(kind="records")
    assert catalog["items"][0]["source_purpose_exclusion_candidate"]
    projection = organization.snapshot()
    member = next(item for item in projection["members"] if item.get("ref", {}).get("record_id") == identity)
    qualifier = member["qualification"]
    assert qualifier["state"] == "qualified"
    assert qualifier["source_evidence_policy"][identity]["source_purpose_exclusion_candidate"]
    write_source_purposes(store.vault, revision=store.head(), classifications=[_classification(source, "unknown")])
    updated = organization.snapshot()
    member = next(item for item in updated["members"] if item.get("ref", {}).get("record_id") == identity)
    assert not member["qualification"]["source_evidence_policy"][identity]["source_purpose_exclusion_candidate"]
