from __future__ import annotations

import copy

import pytest
from test_v2_publication import _bootstrap, _record, _source

from synapse.knowledge import decode_record, encode_record
from synapse.runs import RunManager
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


def _setup(tmp_path):
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host, b"I tried a sketchbook to explore ideas.")
    manager = RunManager(publisher.store.vault)
    run_id = generate_ulid()
    request = {
        "id": generate_ulid(), "mode": "prepare", "owner_instruction_ref": "explicit-save",
        "source_refs": [{"source_id": source["id"], "source_version": source["version"]}],
        "pinned_revision": publisher.store.head(),
        "budget": {"max_minutes": 2, "max_operations": 8, "max_source_expansions": 8, "max_leads": 3, "max_hints": 3, "max_result_characters": 8000},
    }
    capability = host.record_instruction("explicit-save", actions=["prepare"], scope={"run_id": run_id, "source_refs": request["source_refs"], "request_hash": hash_bytes(canonical_json(request))})
    manager.start_preparation(request, capability, run_id=run_id)
    return publisher, host, manager, source, run_id, request, capability


def _question(publisher, source, run_id, *, kind="question"):
    identity = generate_ulid()
    raw = _record(identity, subject_id=identity, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)
    value = decode_record(raw)
    value.update(record_kind=kind, statement="What would help develop the sketchbook idea?")
    return {f"entities/insights/{identity}.md": encode_record(value)}


@pytest.mark.parametrize("status", ["completed", "partial", "cancelled", "failed"])
def test_terminal_preparation_cannot_be_checkpointed_again(tmp_path, status):
    _, _, manager, _, run_id, _, capability = _setup(tmp_path)
    manager.finish(run_id, capability, status=status)
    before = manager.get(run_id)
    with pytest.raises(V2Error, match=f"already {status}"):
        manager.checkpoint(run_id, capability, operations=8, source_expansions=8,
                           trace={"phase": "after-completion"}, state={"forged": True})
    assert manager.get(run_id) == before


def test_preparation_freezes_and_recovers_one_exact_admission(tmp_path):
    publisher, _, manager, source, run_id, request, cap = _setup(tmp_path)
    records = _question(publisher, source, run_id)
    operation, request_id = generate_ulid(), generate_ulid()
    before = publisher.store.head()
    with pytest.raises(V2Error, match="frozen"):
        publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
    assert publisher.store.head() == before
    manager.checkpoint(run_id, cap, operations=1, source_expansions=1)
    manager.freeze_preparation(run_id, cap, records, operation_id=operation, request_id=request_id)
    receipt = publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
    manager.finish(run_id, cap, status="completed")
    assert publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id) == receipt
    restarted = manager.start_preparation(request, cap, run_id=run_id)
    assert restarted["status"] == "completed"
    assert restarted["usage"]["operations"] == 1
    with pytest.raises(V2Error, match="frozen differently"):
        manager.freeze_preparation(run_id, cap, _question(publisher, source, run_id), operation_id=operation, request_id=request_id)


@pytest.mark.parametrize("kind", ["hypothesis", "preference", "decision"])
def test_preparation_cannot_admit_other_claim_kinds(tmp_path, kind):
    publisher, _, manager, source, run_id, _, cap = _setup(tmp_path)
    records = _question(publisher, source, run_id, kind=kind)
    operation, request_id = generate_ulid(), generate_ulid()
    manager.freeze_preparation(run_id, cap, records, operation_id=operation, request_id=request_id)
    with pytest.raises(V2Error, match="only source-anchored"):
        publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
    with pytest.raises(V2Error, match="did not delegate admit"):
        publisher.admit(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)


def test_preparation_scope_and_output_count_cannot_expand(tmp_path):
    publisher, host, manager, source, run_id, request, cap = _setup(tmp_path)
    other = _source(publisher, host, b"Unrelated ceramic glaze notes.", origin="other")
    records = _question(publisher, other, run_id)
    operation, request_id = generate_ulid(), generate_ulid()
    manager.freeze_preparation(run_id, cap, records, operation_id=operation, request_id=request_id)
    with pytest.raises(V2Error, match="outside the requested"):
        publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
    forged = copy.deepcopy(request)
    forged["budget"]["max_operations"] = 9
    with pytest.raises(V2Error):
        manager.start_preparation(forged, cap, run_id=run_id)
    capture = host.record_instruction("explicit-save", actions=["capture"])
    with pytest.raises(V2Error, match="did not delegate prepare"):
        manager.start_preparation(request, capture, run_id=run_id)


def test_changed_source_blocks_preparation_and_capture_overwrite(tmp_path):
    publisher, host, manager, source, run_id, _, cap = _setup(tmp_path)
    records = _question(publisher, source, run_id)
    operation, request_id = generate_ulid(), generate_ulid()
    manager.freeze_preparation(run_id, cap, records, operation_id=operation, request_id=request_id)
    _source(publisher, host, b"The original idea has changed.", source_id=source["id"])
    with pytest.raises(V2Error, match="source changed"):
        publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
    capture = host.record_instruction("explicit-save", actions=["capture"], scope={"capture_targets": {source["origin"]: source["original_hash"]}})
    with pytest.raises(V2Error, match="Source changed"):
        publisher.capture(capture, source, {}, operation_id=generate_ulid(), request_id=generate_ulid(), expected_version=source["version"])


def test_preparation_cannot_exceed_frozen_lead_budget(tmp_path):
    publisher, _, manager, source, run_id, _, cap = _setup(tmp_path)
    records = {}
    for _ in range(4):
        records.update(_question(publisher, source, run_id))
    operation, request_id = generate_ulid(), generate_ulid()
    manager.freeze_preparation(run_id, cap, records, operation_id=operation, request_id=request_id)
    with pytest.raises(V2Error, match="lead or hint budget"):
        publisher.admit_preparation(cap, records, run_id=run_id, operation_id=operation, request_id=request_id)
