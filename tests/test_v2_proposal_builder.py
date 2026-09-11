from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from synapse.knowledge import decode_record, encode_record, record_descriptor
from synapse.proposal_builder import build_proposal
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes, version_for

EXAMPLE = json.loads(
    (Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json").read_text()
)["payload"]


def _store(tmp_path: Path) -> tuple[RevisionStore, dict, bytes]:
    vault = tmp_path / "vault"
    owner_raw = b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOwner\n"
    owner = record_descriptor(owner_raw, path="entities/people/me.md")
    store = RevisionStore(vault)
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"proposal-builder-baseline"),
        objects={owner["version"]: owner_raw},
        initialize=True,
        mutate=lambda manifest, _read: manifest["records"].update({"me": owner}),
    )
    return store, owner, owner_raw


def _change_group(path: str, raw: bytes, *, target_id: str | None = None) -> dict:
    change = {"kind": "replace-record" if target_id else "create-record", "path": path, "raw": raw}
    if target_id:
        change["target_id"] = target_id
    return {
        "id": "g1",
        "requires": [],
        "effects": [
            {"id": "e1", "kind": "mechanical", "meaning": "Keep the exact reviewed change.", "brief_span_start": 0, "brief_span_end": 33},
            {"id": "e2", "kind": "qualification", "meaning": "Retain the second effect.", "brief_span_start": 10, "brief_span_end": 33},
        ],
        "changes": [change],
        "read_set": [],
        "source_preconditions": [],
    }


def test_builder_freezes_v2_bytes_and_one_receipt_per_group(tmp_path: Path) -> None:
    store, owner, _owner_raw = _store(tmp_path)
    payload = copy.deepcopy(EXAMPLE)
    identity = generate_ulid()
    payload.update(
        id=identity,
        subject_id="me",
        record_kind="question",
        availability="suggestion",
        review_status="proposed",
        evidence=[],
        statement="A bounded question.",
    )
    raw = encode_record(payload, name="Question")
    packet, objects = build_proposal(
        store,
        run_id=generate_ulid(),
        brief="Keep this exact reviewed change in the packet.",
        groups=[_change_group(f"entities/insights/{identity}.md", raw)],
    )
    operation = packet["groups"][0]["operations"][0]
    after = decode_record(objects[operation["after_hash"]])
    assert operation["target_id"] == identity
    assert operation["before_hash"] is None
    assert operation["effect_ids"] == ["e1", "e2"]
    assert after["availability"] == "accepted"
    assert after["review_status"] == "proposed"
    assert after["owner_review"]["status"] == "reviewed"
    assert after["owner_review"]["disposition"] == "adopted"
    assert after["owner_review"]["receipt_id"]
    assert packet["semantic_review"] == {"status": "pending"}
    assert packet["version"] == version_for("proposal", packet)
    assert packet["groups"][0]["read_set"] == [{"id": "me", "version": owner["version"]}]


def test_builder_uses_pinned_before_version_and_rejects_verified_bytes(tmp_path: Path) -> None:
    store, owner, owner_raw = _store(tmp_path)
    changed = owner_raw.replace(b"Owner", b"Owner updated")
    group = _change_group(owner["path"], changed, target_id="me")
    group["changes"][0]["before_version"] = owner["version"]
    packet, _objects = build_proposal(
        store,
        run_id=generate_ulid(),
        brief="Keep this exact reviewed change in the packet.",
        groups=[group],
    )
    assert packet["groups"][0]["operations"][0]["before_hash"] == owner["version"]

    verified = owner_raw.replace(b"review_status: proposed", b"review_status: verified")
    verified_group = _change_group(owner["path"], verified, target_id="me")
    with pytest.raises(V2Error) as caught:
        build_proposal(
            store,
            run_id=generate_ulid(),
            brief="Keep this exact reviewed change in the packet.",
            groups=[verified_group],
        )
    assert caught.value.code == "approval-required"


def test_builder_rejects_uncovered_or_out_of_range_effects(tmp_path: Path) -> None:
    store, _owner, owner_raw = _store(tmp_path)
    group = _change_group("entities/people/me.md", owner_raw, target_id="me")
    group["effects"][0]["brief_span_end"] = 999
    with pytest.raises(V2Error) as caught:
        build_proposal(
            store,
            run_id=generate_ulid(),
            brief="Short brief.",
            groups=[group],
        )
    assert caught.value.code == "invalid-span"
