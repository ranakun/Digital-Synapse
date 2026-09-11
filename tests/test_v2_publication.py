from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from synapse.knowledge import decode_record, encode_record, record_descriptor
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher, snapshot_fingerprint
from synapse.revisions import RevisionStore, durable_write
from synapse.source_store import evidence_ref, prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

ROOT = Path(__file__).parents[1]
EXAMPLE = json.loads(
    (ROOT / "docs/v2/contracts/example-knowledge_record.json").read_text(encoding="utf-8")
)["payload"]
LEGACY_PATH = "entities/people/me.md"
LEGACY_RAW = (
    b"---\r\n"
    b"id: me\r\n"
    b"type: person\r\n"
    b"name: Owner\r\n"
    b"review_status: proposed\r\n"
    b"---\r\n"
    b"\r\nSynthetic owner baseline.\r\n"
)


def _source(
    publisher: Publisher,
    host: OwnerHost,
    raw: bytes = b"The synthetic source supports this bounded claim.",
    *,
    origin: str = "fixture-source",
    source_id: str | None = None,
    source_family_id: str | None = None,
) -> dict:
    descriptor, objects = prepare_source(
        raw,
        origin=origin,
        source_id=source_id,
        source_family_id=source_family_id,
        captured_at="2026-09-11T08:00:00Z",
    )
    capability = host.record_instruction(
        "fixture-owner-capture",
        actions=["capture"],
        scope={"capture_targets": {origin: descriptor["original_hash"]}},
    )
    publisher.capture(
        capability,
        descriptor,
        objects,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    return descriptor


def _bootstrap(
    tmp_path: Path,
    *,
    extra_records: dict[str, bytes] | None = None,
    sources: list[dict] | None = None,
    objects: dict[str, bytes] | None = None,
) -> tuple[Publisher, OwnerHost, str]:
    vault = tmp_path / "vault"
    records = {LEGACY_PATH: LEGACY_RAW} | (extra_records or {})
    store = RevisionStore(vault)
    host = OwnerHost(store)
    capability = host.record_instruction(
        "fixture-owner-bootstrap",
        actions=["capture"],
        scope={
            "bootstrap": True,
            "snapshot_hash": snapshot_fingerprint(records, sources or []),
        },
    )
    publisher = Publisher(vault)
    receipt = publisher.bootstrap(
        capability,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        records=records,
        sources=sources,
        objects=objects,
    )
    return publisher, host, receipt["knowledge_revision"]


def _run(
    publisher: Publisher,
    host: OwnerHost,
    *,
    actions: list[str] | None = None,
    subject_ids: list[str] | None = None,
) -> tuple[dict[str, str], str]:
    run_id = generate_ulid()
    capability = host.record_instruction(
        "fixture-owner-run",
        actions=actions or ["investigate", "admit", "stage"],
        scope={"run_id": run_id},
    )
    run = {
        "id": run_id,
        "status": "running",
        "owner_event_id": capability["event_id"],
        "request": {"subject_ids": subject_ids or ["me"]},
    }
    durable_write(publisher.store.root / "runs" / f"{run_id}.json", canonical_json(run))
    return capability, run_id


def _record(
    identity: str,
    *,
    subject_id: str = "me",
    availability: str = "suggestion",
    review_status: str = "proposed",
    owner_review: dict | None = None,
    statement: str = "A synthetic bounded claim.",
    source: dict | None = None,
    read_object=None,
    origin_run_id: str | None = None,
    epistemic_basis: list[str] | None = None,
    applies_from: str | None = None,
    context_refs: list[dict] | None = None,
    dependencies: list[dict] | None = None,
    receipt_id: str | None = None,
) -> bytes:
    payload = copy.deepcopy(EXAMPLE)
    if owner_review is None:
        owner_review = {"status": "not-reviewed", "disposition": "none"}
    elif owner_review.get("status") == "reviewed" and "receipt_id" not in owner_review:
        owner_review = dict(owner_review, receipt_id=receipt_id or generate_ulid())
    payload.update(
        id=identity,
        subject_id=subject_id,
        availability=availability,
        review_status=review_status,
        owner_review=owner_review,
        owner_position="unreviewed",
        statement=statement,
        epistemic_basis=epistemic_basis or ["assistant-hypothesis"],
        evidence=[],
        dependencies=dependencies or [],
        context_refs=context_refs or [],
    )
    if source is not None:
        payload["evidence"] = [
            evidence_ref(
                source,
                read_object,
                0,
                len(read_object(source["text_version"])),
            )
        ]
    if origin_run_id is not None:
        payload["origin_run_id"] = origin_run_id
    if applies_from is not None:
        payload["applies_from"] = applies_from
    return encode_record(payload, name=f"Synthetic {identity}")


def _record_row(raw: bytes, identity: str, *, path: str | None = None) -> dict:
    return record_descriptor(raw, path=path or f"entities/insights/{identity}.md")


def _packet(
    run_id: str,
    base_revision: str,
    specs: list[tuple[str, str, bytes]],
    *,
    requires: dict[str, list[str]] | None = None,
    read_set: list[dict] | None = None,
    source_preconditions: list[dict] | None = None,
    expires_at: str | None = None,
    brief: str | None = None,
    uncovered_effect: bool = False,
) -> dict:
    requires = requires or {}
    brief = brief or "Adopt the explicitly reviewed synthetic changes."
    groups = []
    for group_id, identity, raw in specs:
        effect = {
            "id": f"effect-{group_id}-{identity}",
            "kind": "new-claim",
            "meaning": f"Adopt the exact reviewed bytes for {identity}.",
            "brief_span_start": 0,
            "brief_span_end": len(brief),
        }
        effects = [effect]
        if uncovered_effect and not groups and group_id == specs[0][0]:
            effects.append(
                {
                    "id": "uncovered-effect",
                    "kind": "correction",
                    "meaning": "This effect has no mapped operation.",
                    "brief_span_start": 0,
                    "brief_span_end": len(brief),
                }
            )
        operation = {
            "id": f"operation-{group_id}-{identity}",
            "kind": "create-record",
            "target_id": identity,
            "before_hash": None,
            "after_hash": hash_bytes(raw),
            "after_path": f"entities/insights/{identity}.md",
            "effect_ids": [effect["id"]],
        }
        existing = next((group for group in groups if group["id"] == group_id), None)
        if existing is None:
            groups.append(
                {
                    "id": group_id,
                    "requires": requires.get(group_id, []),
                    "effects": effects,
                    "operations": [operation],
                    "read_set": [],
                    "source_preconditions": [],
                }
            )
        else:
            existing["effects"].extend(effects)
            existing["operations"].append(operation)
    return {
        "id": generate_ulid(),
        "version": "0" * 64,
        "run_id": run_id,
        "base_revision": base_revision,
        "brief": brief,
        "presented_group_ids": list(dict.fromkeys(group_id for group_id, _identity, _raw in specs)),
        "groups": groups,
        "read_set": read_set or [],
        "source_preconditions": source_preconditions or [],
        "semantic_review": {"status": "pending"},
        **({"expires_at": expires_at} if expires_at else {}),
    }


def _stage_and_approve(
    publisher: Publisher,
    host: OwnerHost,
    run_capability: dict[str, str],
    packet: dict,
    *,
    selected: list[str] | None = None,
    reviewer=None,
) -> tuple[dict, dict[str, str]]:
    clean_packet = {key: value for key, value in packet.items() if key != "_objects"}
    staged = publisher.stage(
        run_capability,
        clean_packet,
        packet["_objects"],
        semantic_reviewer=reviewer
        or (
            lambda value: {
                "passed": True,
                "proposal_version": value["proposal_version"],
                "reason": "Fixture comparison passed.",
            }
        ),
    )
    selected = selected or clean_packet["presented_group_ids"]
    approval = host.approve_displayed(
        staged,
        selected,
        displayed_brief=staged["brief"],
        owner_message_ref="fixture-owner-approval",
    )
    return staged, approval


def _stage(publisher: Publisher, capability: dict[str, str], packet: dict, reviewer) -> dict:
    return publisher.stage(
        capability,
        {key: value for key, value in packet.items() if key != "_objects"},
        packet["_objects"],
        semantic_reviewer=reviewer,
    )


def _attach_objects(packet: dict, raws: list[bytes]) -> dict:
    packet = copy.deepcopy(packet)
    packet["_objects"] = {hash_bytes(raw): raw for raw in raws}
    return packet


def test_trusted_owner_instruction_is_required_for_bootstrap_and_approval(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)

    with pytest.raises(V2Error, match="Approval must bind"):
        host.record_instruction("worker-fake", actions=["approve"])
    with pytest.raises(V2Error):
        publisher.publish(
            {"approved": True},
            generate_ulid(),
            "0" * 64,
            ["g1"],
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )
    with pytest.raises(V2Error):
        host.revoke({"event_id": generate_ulid(), "token": "fake"})


def test_reviewed_record_without_receipt_id_remains_schema_invalid() -> None:
    raw = _record(
        generate_ulid(),
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
    )
    payload = decode_record(raw)
    del payload["owner_review"]["receipt_id"]
    with pytest.raises(V2Error):
        encode_record(payload, name="Missing receipt fixture")


def test_capture_binds_original_bytes_and_source_family(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    raw = b"original capture bytes"
    descriptor, objects = prepare_source(
        raw,
        origin="exact-export",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    capability = host.record_instruction(
        "fixture-capture-authority",
        actions=["capture"],
        scope={"capture_targets": {"exact-export": descriptor["original_hash"]}},
    )
    receipt = publisher.capture(
        capability,
        descriptor,
        objects,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert receipt["kind"] == "capture"
    assert publisher.store.read_object(descriptor["original_hash"]) == raw
    assert publisher.store.manifest()["sources"][descriptor["id"]] == descriptor["version"]

    changed, changed_objects = prepare_source(
        b"changed bytes",
        origin="exact-export",
        source_id=descriptor["id"],
        source_family_id=descriptor["source_family_id"],
        captured_at="2026-09-11T08:01:00Z",
    )
    with pytest.raises(V2Error, match="evidence family"):
        wrong_family, wrong_family_objects = prepare_source(
            b"different family",
            origin="exact-export",
            source_id=descriptor["id"],
            source_family_id=generate_ulid(),
            captured_at="2026-09-11T08:02:00Z",
        )
        family_capability = host.record_instruction(
            "fixture-capture-family-change",
            actions=["capture"],
            scope={"capture_targets": {"exact-export": wrong_family["original_hash"]}},
        )
        publisher.capture(
            family_capability,
            wrong_family,
            wrong_family_objects,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )
    with pytest.raises(V2Error, match="these original bytes"):
        publisher.capture(
            capability,
            changed,
            changed_objects,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )


def test_admitted_suggestions_keep_exact_evidence_and_reject_future_observation(
    tmp_path: Path,
) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    suggestion_id = generate_ulid()
    suggestion_raw = _record(
        suggestion_id,
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    receipt = publisher.admit(
        run_capability,
        {f"entities/insights/{suggestion_id}.md": suggestion_raw},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    row = publisher.store.manifest()["records"][suggestion_id]
    assert receipt["kind"] == "suggestion-admission"
    assert row["availability"] == "suggestion"
    assert publisher.store.read_record(suggestion_id)["availability"] == "suggestion"
    assert (
        decode_record(suggestion_raw)["evidence"][0]["source_family_id"]
        == source["source_family_id"]
    )
    assert publisher.store.manifest()["records"]["me"]["version"] == hash_bytes(LEGACY_RAW)

    future_id = generate_ulid()
    future_raw = _record(
        future_id,
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
        epistemic_basis=["interaction-observation"],
        applies_from="2999-01-01",
    )
    with pytest.raises(V2Error, match="future occurrence"):
        publisher.admit(
            run_capability,
            {f"entities/insights/{future_id}.md": future_raw},
            run_id=run_id,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )


def test_admission_enforces_requested_subject_scope(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host, subject_ids=["me"])
    identity = generate_ulid()
    raw = _record(
        identity,
        subject_id=generate_ulid(),
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    with pytest.raises(V2Error, match="subject scope"):
        publisher.admit(
            run_capability,
            {f"entities/insights/{identity}.md": raw},
            run_id=run_id,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )


def test_suggestion_revision_requires_exact_version_and_preserves_old_read(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    original = _record(
        identity,
        statement="Original synthetic possibility.",
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    first_receipt = publisher.admit(
        run_capability,
        {f"entities/insights/{identity}.md": original},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    original_version = publisher.store.manifest()["records"][identity]["version"]
    revised = _record(
        identity,
        statement="Revised synthetic possibility.",
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    publisher.admit(
        run_capability,
        {f"entities/insights/{identity}.md": revised},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        expected_versions={identity: original_version},
    )
    assert publisher.store.read_record(identity, first_receipt["knowledge_revision"])["statement"] == (
        "Original synthetic possibility."
    )
    assert publisher.store.read_record(identity)["statement"] == "Revised synthetic possibility."

    mismatched = _record(
        identity,
        statement="Third synthetic possibility.",
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    with pytest.raises(V2Error, match="exact unreviewed suggestion"):
        publisher.admit(
            run_capability,
            {f"entities/insights/{identity}.md": mismatched},
            run_id=run_id,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
            expected_versions={identity: hash_bytes(b"wrong version")},
        )

    accepted = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    with pytest.raises(V2Error, match="Admission cannot adopt"):
        publisher.admit(
            run_capability,
            {f"entities/insights/{identity}.md": accepted},
            run_id=run_id,
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
            expected_versions={identity: publisher.store.manifest()["records"][identity]["version"]},
        )


def test_suggestion_cannot_be_staged_as_accepted_adoption(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity, source=source, read_object=publisher.store.read_object, origin_run_id=run_id
    )
    publisher.admit(
        run_capability,
        {f"entities/insights/{identity}.md": raw},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    row = publisher.store.manifest()["records"][identity]
    packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)]), [raw]
    )
    packet["groups"][0]["operations"][0]["kind"] = "replace-record"
    packet["groups"][0]["operations"][0]["before_hash"] = row["version"]
    with pytest.raises(V2Error, match="exact accepted target"):
        _stage(
            publisher,
            run_capability,
            packet,
            lambda value: {
                "passed": True,
                "proposal_version": value["proposal_version"],
                "reason": "passed",
            },
        )


@pytest.mark.parametrize(
    "reviewer",
    [None, lambda _value: {"passed": False, "proposal_version": "0" * 64, "reason": "failed"}],
)
def test_stage_requires_independent_semantic_review(tmp_path: Path, reviewer) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)]), [raw]
    )
    with pytest.raises(V2Error):
        _stage(publisher, run_capability, packet, reviewer)
    assert not (publisher.store.root / "proposals" / packet["id"]).exists()


def test_stage_rejects_uncovered_effect_and_tampered_stored_packet(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)], uncovered_effect=True),
        [raw],
    )
    with pytest.raises(V2Error, match="complete coverage"):
        _stage(
            publisher,
            run_capability,
            packet,
            lambda value: {
                "passed": True,
                "proposal_version": value["proposal_version"],
                "reason": "passed",
            },
        )

    valid_packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)]), [raw]
    )
    staged, _ = _stage_and_approve(publisher, host, run_capability, valid_packet)
    proposal_path = publisher.store.root / "proposals" / staged["id"] / f"{staged['version']}.json"
    tampered = copy.deepcopy(staged)
    tampered["brief"] = "Tampered displayed meaning."
    durable_write(proposal_path, canonical_json(tampered))
    with pytest.raises(V2Error, match="Proposal bytes do not match"):
        publisher.proposal(staged["id"], staged["version"])


def test_publish_exact_partial_selection_allows_unselected_independent_drift(
    tmp_path: Path,
) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    raws = [
        _record(
            generate_ulid(),
            availability="accepted",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        for _ in range(3)
    ]
    identities = [decode_record(raw)["id"] for raw in raws]
    packet = _attach_objects(
        _packet(
            run_id,
            publisher.store.head(),
            list(zip(["g1", "g2", "g3"], identities, raws, strict=True)),
        ),
        raws,
    )
    staged, approval = _stage_and_approve(
        publisher, host, run_capability, packet, selected=["g1", "g2"]
    )

    aliases = {
        group_id: decode_record(raw)["owner_review"]["receipt_id"]
        for group_id, _identity, raw in zip(["g1", "g2", "g3"], identities, raws, strict=True)
    }
    for alias in aliases.values():
        with pytest.raises(V2Error, match="not committed"):
            publisher.review_receipt(alias)
    result = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["g1", "g2"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert result["receipt"]["selected_group_ids"] == ["g1", "g2"]
    assert set(publisher.store.manifest()["records"]) >= {"me", identities[0], identities[1]}
    assert identities[2] not in publisher.store.manifest()["records"]
    for group_id in ["g1", "g2"]:
        resolved = publisher.review_receipt(aliases[group_id])
        assert resolved["id"] == aliases[group_id]
        assert resolved["selected_group_ids"] == ["g1", "g2"]
    with pytest.raises(V2Error, match="not committed"):
        publisher.review_receipt(aliases["g3"])

    other_run_capability, other_run_id = _run(publisher, host)
    independent_packet = _attach_objects(
        _packet(other_run_id, publisher.store.head(), [("g3", identities[2], raws[2])]), [raws[2]]
    )
    other_staged, other_approval = _stage_and_approve(
        publisher, host, other_run_capability, independent_packet
    )
    publisher.publish(
        other_approval,
        other_staged["id"],
        other_staged["version"],
        ["g3"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert publisher.review_receipt(aliases["g3"])["id"] == aliases["g3"]


def test_reserved_review_alias_is_bound_once_across_commits(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    alias = generate_ulid()

    first_run, first_run_id = _run(publisher, host)
    first_id = generate_ulid()
    first_raw = _record(
        first_id,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        receipt_id=alias,
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=first_run_id,
    )
    first_packet = _attach_objects(
        _packet(first_run_id, publisher.store.head(), [("g1", first_id, first_raw)]), [first_raw]
    )
    first_staged, first_approval = _stage_and_approve(
        publisher, host, first_run, first_packet
    )
    first_result = publisher.publish(
        first_approval,
        first_staged["id"],
        first_staged["version"],
        ["g1"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )

    second_run, second_run_id = _run(publisher, host)
    second_id = generate_ulid()
    second_raw = _record(
        second_id,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        receipt_id=alias,
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=second_run_id,
    )
    second_packet = _attach_objects(
        _packet(second_run_id, publisher.store.head(), [("g2", second_id, second_raw)]), [second_raw]
    )
    second_staged, second_approval = _stage_and_approve(
        publisher, host, second_run, second_packet
    )
    with pytest.raises(V2Error, match="belongs to another group"):
        publisher.publish(
            second_approval,
            second_staged["id"],
            second_staged["version"],
            ["g2"],
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )
    assert publisher.review_receipt(alias)["knowledge_revision"] == first_result["receipt"][
        "knowledge_revision"
    ]


def test_review_receipts_are_fresh_per_group_and_shared_within_group(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    shared_alias, independent_alias = generate_ulid(), generate_ulid()
    ids = [generate_ulid() for _ in range(3)]
    raws = [
        _record(
            ids[0],
            availability="accepted",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            receipt_id=shared_alias,
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        ),
        _record(
            ids[1],
            availability="accepted",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            receipt_id=shared_alias,
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        ),
        _record(
            ids[2],
            availability="accepted",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            receipt_id=independent_alias,
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        ),
    ]
    assert decode_record(raws[0])["owner_review"]["receipt_id"] == decode_record(raws[1])[
        "owner_review"
    ]["receipt_id"]
    assert shared_alias != independent_alias
    packet = _attach_objects(
        _packet(
            run_id,
            publisher.store.head(),
            [("g1", ids[0], raws[0]), ("g1", ids[1], raws[1]), ("g2", ids[2], raws[2])],
        ),
        raws,
    )
    staged, approval = _stage_and_approve(
        publisher, host, run_capability, packet, selected=["g1"]
    )
    with pytest.raises(V2Error, match="not committed"):
        publisher.review_receipt(shared_alias)
    result = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["g1"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert publisher.review_receipt(shared_alias)["id"] == shared_alias
    with pytest.raises(V2Error, match="not committed"):
        publisher.review_receipt(independent_alias)
    assert result["receipt"]["selected_group_ids"] == ["g1"]


@pytest.mark.parametrize("blocker", ["source", "checkout", "expiry"])
def test_selected_material_drift_external_edit_and_expiry_block_publish(
    tmp_path: Path, blocker: str
) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    expires = (
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        if blocker == "expiry"
        else None
    )
    packet = _attach_objects(
        _packet(
            run_id,
            publisher.store.head(),
            [("g1", identity, raw)],
            source_preconditions=[{"source_id": source["id"], "source_version": source["version"]}]
            if blocker == "source"
            else [],
            expires_at=expires,
        ),
        [raw],
    )
    staged, approval = _stage_and_approve(publisher, host, run_capability, packet)
    if blocker == "source":
        _source(
            publisher,
            host,
            b"new source version",
            origin="fixture-source",
            source_id=source["id"],
            source_family_id=source["source_family_id"],
        )
    elif blocker == "checkout":
        checkout = publisher.store.checkout_path(f"entities/insights/{identity}.md")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        checkout.write_bytes(b"external edit")
    else:
        time.sleep(1.1)
    with pytest.raises(V2Error) as caught:
        publisher.publish(
            approval,
            staged["id"],
            staged["version"],
            ["g1"],
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )
    assert caught.value.code in {
        "stale-selection",
        "external-edit-conflict",
        "precondition-expired",
    }
    assert identity not in publisher.store.manifest()["records"]


@pytest.mark.parametrize("shape", ["hidden", "overlap", "cycle"])
def test_hidden_prerequisite_overlap_and_cycle_are_rejected(tmp_path: Path, shape: str) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    raws = [
        _record(
            generate_ulid(),
            availability="accepted",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        for _ in range(2)
    ]
    ids = [decode_record(raw)["id"] for raw in raws]
    packet = _packet(
        run_id,
        publisher.store.head(),
        [("g1", ids[0], raws[0]), ("g2", ids[1], raws[1])],
        requires={
            "g1": ["g2"] if shape in {"hidden", "cycle"} else [],
            "g2": ["g1"] if shape == "cycle" else [],
        },
    )
    if shape == "hidden":
        packet["presented_group_ids"] = ["g1"]
    elif shape == "overlap":
        packet["groups"][1]["operations"][0]["target_id"] = ids[0]
        packet["groups"][1]["operations"][0]["after_hash"] = hash_bytes(raws[0])
        packet["_objects"] = {hash_bytes(raw): raw for raw in raws}
    packet = _attach_objects(packet, raws)
    with pytest.raises(V2Error):
        _stage(
            publisher,
            run_capability,
            packet,
            lambda value: {
                "passed": True,
                "proposal_version": value["proposal_version"],
                "reason": "passed",
            },
        )


@pytest.mark.parametrize("case", ["mint", "changed-verified"])
def test_adoption_cannot_mint_or_inherit_verified_status(tmp_path: Path, case: str) -> None:
    source_raw = b"The verified baseline has exact synthetic evidence."
    descriptor, source_objects = prepare_source(
        source_raw,
        origin="verified-source",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    verified_id = generate_ulid()
    verified_raw = _record(
        verified_id,
        availability="accepted",
        review_status="verified",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=descriptor,
        read_object=source_objects.__getitem__,
    )
    publisher, host, _ = _bootstrap(
        tmp_path,
        extra_records={f"entities/insights/{verified_id}.md": verified_raw},
        sources=[descriptor],
        objects=source_objects,
    )
    run_capability, run_id = _run(publisher, host)
    if case == "mint":
        target_id = generate_ulid()
        raw = _record(
            target_id,
            availability="accepted",
            review_status="verified",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            source=descriptor,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        packet = _packet(run_id, publisher.store.head(), [("g1", target_id, raw)])
    else:
        raw = _record(
            verified_id,
            availability="accepted",
            review_status="verified",
            owner_review={"status": "reviewed", "disposition": "adopted"},
            statement="Changed verified meaning.",
            source=descriptor,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        packet = _packet(run_id, publisher.store.head(), [("g1", verified_id, raw)])
        packet["groups"][0]["operations"][0]["kind"] = "replace-record"
        packet["groups"][0]["operations"][0]["before_hash"] = publisher.store.manifest()["records"][
            verified_id
        ]["version"]
    packet = _attach_objects(packet, [raw])
    with pytest.raises(V2Error, match="verification|inherit"):
        _stage(
            publisher,
            run_capability,
            packet,
            lambda value: {
                "passed": True,
                "proposal_version": value["proposal_version"],
                "reason": "passed",
            },
        )


def test_revocation_blocks_before_commit_and_reports_compensation_after_commit(
    tmp_path: Path,
) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)]), [raw]
    )
    staged, approval = _stage_and_approve(publisher, host, run_capability, packet)
    assert host.revoke(approval)["status"] == "revoked"
    with pytest.raises(V2Error, match="revoked"):
        publisher.publish(
            approval,
            staged["id"],
            staged["version"],
            ["g1"],
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )

    # A fresh approval reaches the durable commit first; revocation then offers compensation.
    approval = host.approve_displayed(
        staged,
        ["g1"],
        displayed_brief=staged["brief"],
        owner_message_ref="fixture-owner-approval-2",
    )
    result = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["g1"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert host.revoke(approval)["status"] == "already-committed"
    assert result["receipt"]["kind"] == "adoption"


def test_lost_response_retry_returns_original_receipt_and_current_readiness(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    identity = generate_ulid()
    raw = _record(
        identity,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    packet = _attach_objects(
        _packet(run_id, publisher.store.head(), [("g1", identity, raw)]), [raw]
    )
    staged, approval = _stage_and_approve(publisher, host, run_capability, packet)
    operation_id, request_id = generate_ulid(), generate_ulid()

    def lost_response(phase: str) -> None:
        if phase == "head":
            raise RuntimeError("simulated lost response after HEAD")

    with pytest.raises(RuntimeError, match="lost response"):
        publisher.publish(
            approval,
            staged["id"],
            staged["version"],
            ["g1"],
            operation_id=operation_id,
            request_id=request_id,
            fault=lost_response,
        )
    original_revision = publisher.store.head()
    original_receipt = publisher.store.receipt(operation_id)
    assert original_receipt is not None

    other_run, other_run_id = _run(publisher, host)
    other_id = generate_ulid()
    other_raw = _record(
        other_id,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=other_run_id,
    )
    other_packet = _attach_objects(
        _packet(other_run_id, publisher.store.head(), [("g1", other_id, other_raw)]), [other_raw]
    )
    other_staged, other_approval = _stage_and_approve(publisher, host, other_run, other_packet)
    publisher.publish(
        other_approval,
        other_staged["id"],
        other_staged["version"],
        ["g1"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    retry = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["g1"],
        operation_id=operation_id,
        request_id=request_id,
    )
    assert retry["receipt"]["knowledge_revision"] == original_revision
    assert retry["receipt"]["id"] == original_receipt["id"]
    assert retry["readiness"]["revision"] == publisher.store.head()
    assert retry["readiness"]["revision"] != original_revision


def test_dispositions_are_distinct_and_retry_is_idempotent_after_current_change(
    tmp_path: Path,
) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    raws = [
        _record(
            generate_ulid(),
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        for _ in range(2)
    ]
    ids = [decode_record(raw)["id"] for raw in raws]
    publisher.admit(
        run_capability,
        {f"entities/insights/{identity}.md": raw for identity, raw in zip(ids, raws, strict=True)},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    capabilities = []
    for identity, action in zip(ids, ["dismissed", "declined-adoption"], strict=True):
        row = publisher.store.manifest()["records"][identity]
        capabilities.append(
            (
                host.record_instruction(
                    "fixture-owner-disposition",
                    actions=["dispose"],
                    scope={"disposition": action, "records": {identity: row["version"]}},
                ),
                action,
            )
        )
    operation_id, request_id = generate_ulid(), generate_ulid()
    dismissed = publisher.disposition(
        capabilities[0][0],
        [ids[0]],
        capabilities[0][1],
        operation_id=operation_id,
        request_id=request_id,
    )
    _source(publisher, host, b"unrelated current source", origin="unrelated-source")
    assert (
        publisher.disposition(
            capabilities[0][0],
            [ids[0]],
            capabilities[0][1],
            operation_id=operation_id,
            request_id=request_id,
        )
        == dismissed
    )
    publisher.disposition(
        capabilities[1][0],
        [ids[1]],
        capabilities[1][1],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert publisher.store.manifest()["records"][ids[0]]["disposition"] == "dismissed"
    assert publisher.store.manifest()["records"][ids[1]]["disposition"] == "declined-adoption"


def test_context_reference_to_false_claim_is_not_a_premise(tmp_path: Path) -> None:
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host)
    run_capability, run_id = _run(publisher, host)
    false_id = generate_ulid()
    false_raw = _record(
        false_id, source=source, read_object=publisher.store.read_object, origin_run_id=run_id
    )
    publisher.admit(
        run_capability,
        {f"entities/insights/{false_id}.md": false_raw},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    context_id = generate_ulid()
    context_raw = _record(
        context_id,
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
        context_refs=[
            {
                "id": false_id,
                "version": hash_bytes(false_raw),
                "role": "contradicts",
                "scope": "The suggestion is a false claim under review.",
            }
        ],
    )
    publisher.admit(
        run_capability,
        {f"entities/insights/{context_id}.md": context_raw},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    assert publisher.store.read_record(context_id)["dependencies"] == []
