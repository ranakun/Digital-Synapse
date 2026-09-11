from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_v2_organization import _publish_topics, _v2_record, _vault

from synapse.gateway import Gateway
from synapse.knowledge import decode_record, encode_record, record_descriptor
from synapse.organization import Organization, OrganizationConfig, _projection_integrity
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes


def _projection_path(vault: Path, snapshot: dict) -> Path:
    return vault / ".synapse" / "organization" / f"{snapshot['organization_revision']}.json"


def _publish_records(store: RevisionStore, rows: list[tuple[dict, bytes]]) -> None:
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"organization-regression-records"),
        objects={row["version"]: raw for row, raw in rows}, initialize=True,
        mutate=lambda manifest, _read: manifest["records"].update({row["id"]: row for row, _raw in rows}),
    )


@pytest.mark.parametrize("rebuild", [False, True], ids=["implicit", "forced"])
@pytest.mark.parametrize("corruption", ["missing", "json", "list", "checksum", "semantic"])
def test_current_rebuild_repairs_projection_but_explicit_read_refuses(
    tmp_path: Path, corruption: str, rebuild: bool,
) -> None:
    vault, _store = _vault(tmp_path, with_source=False)
    organization = Organization(Gateway(vault))
    original = organization.snapshot()
    borrowed = organization._snapshot_view()
    path = _projection_path(vault, original)
    if corruption == "missing":
        path.unlink()
    elif corruption == "json":
        path.write_text("{corrupt", encoding="utf-8")
    elif corruption == "list":
        path.write_text("[]", encoding="utf-8")
    else:
        damaged = json.loads(path.read_text())
        if corruption == "checksum":
            damaged["coverage"]["input_records"] += 1
        else:
            damaged["semantic"] = []
            damaged["projection_hash"] = _projection_integrity(damaged)
        path.write_text(json.dumps(damaged), encoding="utf-8")
    corrupt_bytes = path.read_bytes() if path.exists() else None

    for reader in (organization, Organization(Gateway(vault))):
        with pytest.raises(V2Error) as caught:
            reader.snapshot(organization_revision=original["organization_revision"])
        assert caught.value.code == "revision-unavailable"
        assert (path.read_bytes() if path.exists() else None) == corrupt_bytes
    assert organization.snapshot(rebuild=rebuild) == original
    assert json.loads(path.read_text()) == original
    assert Organization(Gateway(vault)).snapshot() == original
    assert borrowed == original
    with pytest.raises(TypeError):
        borrowed["coverage"]["input_records"] = -1
    public = organization.snapshot()
    public["members"].clear()
    assert organization._snapshot_view()["members"] == original["members"]


@pytest.mark.parametrize("missing", [False, True], ids=["corrupt", "missing"])
def test_current_rebuild_does_not_repair_explicit_historical_projection(
    tmp_path: Path, missing: bool,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    _publish_topics(store, combined=False, initialize=True)
    parent_organization = Organization(Gateway(store.vault))
    parent = parent_organization.snapshot()
    path = _projection_path(store.vault, parent)
    _publish_topics(store, combined=True)
    if missing:
        path.unlink()
    else:
        path.write_text("{corrupt", encoding="utf-8")
    current = Organization(Gateway(store.vault))
    for reader in (parent_organization, current):
        with pytest.raises(V2Error) as caught:
            reader.snapshot(organization_revision=parent["organization_revision"], rebuild=True)
        assert caught.value.code == "revision-unavailable"
    rebuilt = current.snapshot()
    assert rebuilt["knowledge_revision"] != parent["knowledge_revision"]
    assert len(rebuilt["areas"]) == 1
    assert rebuilt["lineage"] == []
    assert (not path.exists()) if missing else path.read_text() == "{corrupt"


@pytest.mark.parametrize("failure", ["withheld", "incomplete"])
def test_qualification_precedes_duplicate_representative_and_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    accepted = _v2_record(generate_ulid(), name="Lighthouse lens", availability="accepted", disposition="none")
    suggestion = _v2_record(generate_ulid(), name="Lighthouse lens", availability="suggestion", disposition="none")
    _publish_records(store, [accepted, suggestion])
    gateway = Gateway(store.vault)
    unit = gateway._unit

    def qualify(identity: str, **kwargs) -> dict:
        result = unit(identity, **kwargs)
        if identity == accepted[0]["id"]:
            result.update(withheld=failure == "withheld", complete=failure != "incomplete")
        return result

    monkeypatch.setattr(gateway, "_unit", qualify)
    organization = Organization(gateway)
    feature_ids = []
    features = organization._cached_features

    def observed_features(member: dict, *, model: str):
        feature_ids.append(member["id"])
        return features(member, model=model)

    monkeypatch.setattr(organization, "_cached_features", observed_features)
    snapshot = organization.snapshot()
    assert feature_ids == [f"record:{suggestion[0]['id']}"]
    assert [member["ref"]["record_id"] for member in snapshot["members"]] == [suggestion[0]["id"]]
    assert snapshot["members"][0]["qualification"]["availability"] == "suggestion"
    assert snapshot["members"][0]["qualification"]["owner_position"] == "unreviewed"
    assert snapshot["coverage"]["withheld_records"] == 1
    assert snapshot["coverage"]["withheld_duplicate_groups"] == 0
    assert snapshot["coverage"]["duplicate_records"] == 0
    assert snapshot["coverage"]["duplicate_record_refs"] == []
    assert snapshot["areas"] == []
    for descriptor, raw in (accepted, suggestion):
        assert store.read_object(descriptor["version"]) == raw
        assert gateway.view.markdown(descriptor["id"])["markdown"]


def test_all_withheld_duplicates_report_the_whole_excluded_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    rows = [
        _v2_record(generate_ulid(), name="Lens coating", availability=availability, disposition="none")
        for availability in ("accepted", "suggestion", "suggestion")
    ]
    _publish_records(store, rows)
    gateway = Gateway(store.vault)
    monkeypatch.setattr(gateway, "_unit", lambda *_args, **_kwargs: {"complete": False, "withheld": True})
    organization = Organization(gateway)

    def unexpected_features(*_args, **_kwargs):
        pytest.fail("Withheld material reached feature extraction")

    monkeypatch.setattr(organization, "_cached_features", unexpected_features)
    snapshot = organization.snapshot()
    assert snapshot["members"] == []
    assert snapshot["areas"] == []
    assert snapshot["coverage"]["withheld_records"] == 3
    assert snapshot["coverage"]["withheld_duplicate_groups"] == 1
    assert snapshot["coverage"]["duplicate_records"] == 0
    assert snapshot["coverage"]["duplicate_record_refs"] == []


def test_eligible_duplicate_refs_all_target_final_accepted_representative(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "vault")
    identities = sorted(generate_ulid() for _ in range(4))
    # Stable ID alone would choose the suggestion. Acceptance wins first,
    # then stable ID breaks the tie between the two accepted candidates.
    rows = [
        _v2_record(identity, name="Prism alignment", availability=availability, disposition="none")
        for identity, availability in zip(identities, ("suggestion", "accepted", "accepted", "suggestion"), strict=True)
    ]
    _publish_records(store, rows)
    snapshot = Organization(Gateway(store.vault)).snapshot()
    assert [member["ref"]["record_id"] for member in snapshot["members"]] == [identities[1]]
    assert snapshot["coverage"]["duplicate_record_refs"] == [
        {"duplicate_id": identity, "representative_id": identities[1]}
        for identity in identities if identity != identities[1]
    ]
    assert snapshot["coverage"]["duplicate_records"] == 3
    assert snapshot["coverage"]["withheld_records"] == 0
    assert snapshot["coverage"]["input_records"] == 1
    assert snapshot["areas"] == []


def test_candidate_exclusions_precede_qualification_and_navigation_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    excluded = [
        _v2_record(generate_ulid(), name=f"Excluded {index}", availability=availability, disposition=disposition)
        for index, (availability, disposition) in enumerate([
            ("draft", "none"), ("suggestion", "dismissed"), ("suggestion", "disputed"),
        ])
    ]
    withdrawn = _v2_record(generate_ulid(), name="Withdrawn", availability="accepted", disposition="none")
    payload = decode_record(withdrawn[1])
    payload["lifecycle"] = "withdrawn"
    raw = encode_record(payload, name="Withdrawn")
    excluded.append((record_descriptor(raw, path=withdrawn[0]["path"]), raw))
    navigation = _v2_record(generate_ulid(), name="Harbor navigation hint", availability="suggestion", disposition="none")
    payload = decode_record(navigation[1])
    payload["record_kind"] = "navigation"
    raw = encode_record(payload, name="Harbor navigation hint")
    navigation = (record_descriptor(raw, path=navigation[0]["path"]), raw)
    _publish_records(store, [*excluded, navigation])
    organization = Organization(Gateway(store.vault))
    qualification_ids = []
    qualify = organization._qualification

    def observed_qualification(identity: str, **kwargs) -> dict:
        qualification_ids.append(identity)
        return qualify(identity, **kwargs)

    monkeypatch.setattr(organization, "_qualification", observed_qualification)
    snapshot = organization.snapshot()
    assert qualification_ids == [navigation[0]["id"]]
    assert [member["ref"]["record_id"] for member in snapshot["members"]] == [navigation[0]["id"]]
    assert snapshot["coverage"]["suppressed_records"] == 3
    assert snapshot["coverage"]["inactive_records"] == 1
    assert snapshot["coverage"]["withheld_records"] == 0
    assert snapshot["coverage"]["input_records"] == 0
    assert snapshot["coverage"]["navigation_hints"] == 1
    assert snapshot["coverage"]["unique_records"] == 0
    assert snapshot["areas"] == []
    for descriptor, raw in excluded:
        assert store.read_object(descriptor["version"]) == raw


@pytest.mark.parametrize("parent_config", [{"max_neighbors": 12}, {"use_local_vectors": True}])
def test_lineage_refuses_real_parent_projection_from_another_config(
    tmp_path: Path, parent_config: dict,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    _publish_topics(store, combined=False, initialize=True)
    parent = Organization(Gateway(store.vault), config=parent_config).snapshot()
    assert len(parent["areas"]) == 2
    assert json.loads(_projection_path(store.vault, parent).read_text())["projection_hash"] == _projection_integrity(parent)
    _publish_topics(store, combined=True)
    child = Organization(Gateway(store.vault)).snapshot()
    assert len(child["areas"]) == 1
    assert child["lineage"] == []


@pytest.mark.parametrize("mismatch", [
    "checksum", "schema", "algorithm", "chunker", "configuration", "model", "method", "state", "requested",
    "fallback", "knowledge-revision", "organization-revision", "filename", "non-sha-filename", "areas",
])
def test_lineage_refuses_corrupt_or_incompatible_parent_projection(
    tmp_path: Path, mismatch: str,
) -> None:
    store = RevisionStore(tmp_path / "vault")
    _publish_topics(store, combined=False, initialize=True)
    parent = Organization(Gateway(store.vault)).snapshot()
    assert len(parent["areas"]) == 2
    path = _projection_path(store.vault, parent)
    candidate = json.loads(path.read_text())
    if mismatch == "schema":
        candidate["projection_schema"] = "organization-projection/3"
    elif mismatch == "algorithm":
        candidate["configuration"]["algorithm"] = "organization-2"
    elif mismatch == "chunker":
        candidate["configuration"]["chunker"] = "paragraph-900-v1"
    elif mismatch == "configuration":
        candidate["configuration"]["max_neighbors"] = 1.5
    elif mismatch == "model":
        candidate["semantic"]["model"] = "foreign-local-model"
    elif mismatch == "method":
        candidate["method"] = "lexical-tfidf+local-vector"
    elif mismatch == "state":
        candidate["semantic"]["state"] = "unavailable"
    elif mismatch == "requested":
        candidate["semantic"]["requested"] = True
    elif mismatch == "fallback":
        candidate["semantic"]["reason"] = "missing-index"
    elif mismatch == "knowledge-revision":
        candidate["knowledge_revision"] = "e" * 64
    elif mismatch == "organization-revision":
        candidate["organization_revision"] = "e" * 64
    elif mismatch in {"filename", "non-sha-filename"}:
        path.unlink()
        path = path.with_name(("e" * 64 if mismatch == "filename" else "foreign-parent") + ".json")
    elif mismatch == "areas":
        candidate["areas"][0]["member_ids"] = {"invalid": "shape"}
    # Metadata mismatches must be rejected even when the JSON has a valid
    # checksum. Every candidate starts from a real retained parent projection.
    candidate["projection_hash"] = "0" * 64 if mismatch == "checksum" else _projection_integrity(candidate)
    path.write_text(json.dumps(candidate), encoding="utf-8")
    _publish_topics(store, combined=True)
    child = Organization(Gateway(store.vault)).snapshot()
    assert len(child["areas"]) == 1
    assert child["lineage"] == []


def test_lineage_uses_matching_truthful_unavailable_vector_fallback(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "vault")
    _publish_topics(store, combined=False, initialize=True)
    config = {"use_local_vectors": True}
    parent = Organization(Gateway(store.vault), config=config).snapshot()
    assert parent["semantic"] == {"requested": True, "state": "unavailable", "model": None}
    _publish_topics(store, combined=True)
    child = Organization(Gateway(store.vault), config=config).snapshot()
    assert len(child["areas"]) == 1
    assert child["lineage"] == [{
        "kind": "merge", "area_id": child["areas"][0]["id"],
        "previous_area_ids": sorted(area["id"] for area in parent["areas"]),
    }]


@pytest.mark.parametrize("field", ["max_neighbors", "min_units", "max_memberships"])
@pytest.mark.parametrize("value", [True, False, "3", None, 3.5, float("nan"), float("inf"), -1, 0, 2**64])
def test_config_rejects_invalid_integer_types_and_ranges(field: str, value) -> None:
    for build in (lambda: OrganizationConfig(**{field: value}), lambda: OrganizationConfig.from_value({field: value})):
        with pytest.raises(V2Error) as caught:
            build()
        assert caught.value.code == "invalid-request"


@pytest.mark.parametrize("field", ["lexical_cosine_min", "local_vector_cosine_min", "community_resolution", "secondary_membership_ratio"])
@pytest.mark.parametrize("value", [True, False, "0.5", None, float("nan"), float("inf"), -float("inf"), -0.1, 0, 1.01])
def test_config_rejects_invalid_score_types_and_ranges(field: str, value) -> None:
    for build in (lambda: OrganizationConfig(**{field: value}), lambda: OrganizationConfig.from_value({field: value})):
        with pytest.raises(V2Error) as caught:
            build()
        assert caught.value.code == "invalid-request"


@pytest.mark.parametrize("config", [
    {"max_neighbors": 25}, {"max_memberships": 4}, {"min_units": 2},
    {"use_local_vectors": 1}, {"use_local_vectors": "false"},
])
def test_config_rejects_outside_bounded_design(config: dict) -> None:
    with pytest.raises(V2Error) as caught:
        OrganizationConfig.from_value(config)
    assert caught.value.code == "invalid-request"


def test_config_accepts_valid_numeric_boundaries_and_derives(tmp_path: Path) -> None:
    config = OrganizationConfig(
        max_neighbors=1, min_units=3, max_memberships=1,
        lexical_cosine_min=1, local_vector_cosine_min=1,
        community_resolution=1, secondary_membership_ratio=1,
    )
    vault, _store = _vault(tmp_path, with_source=False)
    snapshot = Organization(Gateway(vault), config=config).snapshot()
    assert snapshot["projection_schema"] == "organization-projection/6"
    assert snapshot["configuration"]["algorithm"] == "organization-5-louvain-nx3.6.1-text4"
    assert snapshot["configuration"]["chunker"] == "paragraph-900-v2"
    assert snapshot["configuration"]["lexical_cosine_min"] == 1
    assert snapshot["projection_hash"] == _projection_integrity(snapshot)
    assert OrganizationConfig.from_value(config) is config


def test_source_states_describe_real_complete_partial_pending_and_no_text(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "vault")
    sources = [
        prepare_source(b"Harbor sailing journal", origin="sailing.txt"),
        prepare_source(
            b"synthetic partial original", origin="pottery.pdf", media_type="application/pdf",
            text="Kiln clay glaze pottery firing",
            extraction={"method": "supplied-local-fixture", "version": "1", "completeness": "partial"},
        ),
        prepare_source(
            b"synthetic pending original", origin="lens.pdf", media_type="application/pdf",
            extraction={"method": "pending", "version": "1", "completeness": "pending"},
        ),
        prepare_source(
            b"synthetic no-text original", origin="blank.pdf", media_type="application/pdf",
            extraction={"method": "supplied-local-fixture-no-text", "version": "1", "completeness": "failed"},
        ),
    ]
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"organization-source-states"), initialize=True,
        objects={key: value for _descriptor, objects in sources for key, value in objects.items()},
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"] for descriptor, _ in sources}),
            manifest["source_versions"].update({descriptor["version"]: descriptor for descriptor, _ in sources}),
        ),
    )
    snapshot = Organization(Gateway(store.vault)).snapshot()
    by_source = {member["ref"]["source_id"]: member for member in snapshot["members"]}
    assert set(by_source) == {sources[0][0]["id"], sources[1][0]["id"]}
    for index, expected in enumerate([("complete", "captured"), ("partial", "partly-processed")]):
        qualification = by_source[sources[index][0]["id"]]["qualification"]
        assert (qualification["extraction_state"], qualification["processing_state"]) == expected
        assert qualification["availability"] == "source-material"
        assert qualification["assertion"] is False
    assert "partial" in " ".join(by_source[sources[1][0]["id"]]["qualification"]["limitations"])
    assert snapshot["coverage"]["input_sources"] == 4
    assert snapshot["coverage"]["unreadable_sources"] == 2
    assert snapshot["coverage"]["source_states"] == [
        {"extraction_state": extraction, "processing_state": processing, "text_state": text, "count": 1}
        for extraction, processing, text in [
            ("complete", "captured", "readable"), ("failed", "failed", "no-text"),
            ("partial", "partly-processed", "readable"), ("pending", "captured", "no-text"),
        ]
    ]
    for descriptor, objects in sources:
        assert store.read_object(descriptor["original_hash"]) == objects[descriptor["original_hash"]]


@pytest.mark.parametrize("channels, phrase", [
    (["lexical"], "lexical similarity"), (["vector"], "local vector similarity"),
    (["lexical", "vector"], "lexical and local vector similarity"),
])
def test_aggregate_similarity_explanation_reports_contributing_methods(
    tmp_path: Path, channels: list[str], phrase: str,
) -> None:
    vault, _store = _vault(tmp_path, with_source=False)
    organization = Organization(Gateway(vault))
    members = [
        {"id": "record:r1", "kind": "record", "record_id": "r1"},
        {"id": "record:r2", "kind": "record", "record_id": "r2"},
    ]
    areas = [
        {"id": "area:left", "member_ids": ["record:r1"]},
        {"id": "area:right", "member_ids": ["record:r2"]},
    ]
    links = organization._links(areas, members, [], {0: [(1, .8, channel) for channel in channels]})
    assert len(links) == 1
    assert links[0]["channel"] == "similarity"
    assert links[0]["member_ids"] == ["record:r1", "record:r2"]
    assert links[0]["explanation"] == f"Retained material has qualifying {phrase} across these areas."
