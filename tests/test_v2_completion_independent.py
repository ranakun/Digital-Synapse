"""Bounded independent checks against the frozen final core implementation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_v2_organization_completion import MeaningFixture, _meaning_vault
from test_v2_read_view import _commit, _legacy

from synapse.gateway import Gateway
from synapse.knowledge import encode_record, record_descriptor
from synapse.organization import Organization
from synapse.source_store import evidence_ref, prepare_source, read_passage
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes
from synapse.v2_semantic import (
    SemanticRuntime,
    _text_chunks,
    build_index,
    register_runtime,
    source_passages,
)

ROOT = Path(__file__).resolve().parents[1]


def _retained_bytes(store):
    return {
        str(path.relative_to(store.root)): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def _assert_references_and_counts(snapshot):
    members = {member["id"]: member for member in snapshot["members"]}
    areas = {area["id"]: area for area in snapshot["areas"]}
    memberships = 0
    for area in areas.values():
        selected = [members[identity] for identity in area["member_ids"]]
        records = {
            member["ref"]["record_id"]
            for member in selected
            if member["kind"] == "record"
            and member["qualification"].get("record_kind") != "navigation"
        }
        sources = {
            member["ref"]["source_id"]
            for member in selected
            if member["kind"] == "source"
        }
        assert area["coverage"]["unique_records"] == len(records)
        assert area["coverage"]["unique_sources"] == len(sources)
        assert area["coverage"]["memberships"] == len(selected)
        assert area["summary"]["member_ids"] == area["member_ids"]
        memberships += len(selected)
        for member in selected:
            assert area["id"] in member["area_ids"]
    for member in members.values():
        assert len(member["area_ids"]) <= snapshot["configuration"]["max_memberships"]
        for area_id in member["area_ids"]:
            assert member["id"] in areas[area_id]["member_ids"]
    assert memberships == snapshot["coverage"]["memberships"]
    assert set(snapshot["loose"]) == {
        member["id"] for member in members.values() if not member["area_ids"]
    }


def test_louvain_cold_and_cached_features_agree_across_processes(tmp_path):
    store, embedder, runtime, _report = _meaning_vault(tmp_path)
    try:
        retained = _retained_bytes(store)
        expected = Organization(Gateway(store.vault)).snapshot(rebuild=True)
        _assert_references_and_counts(expected)
        assert all(area["coverage"]["unique_records"] >= 3 for area in expected["areas"])
        script = """
import json, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_v2_organization_completion import MeaningFixture
from synapse.gateway import Gateway
from synapse.organization import Organization
from synapse.v2_semantic import SemanticRuntime, register_runtime
import networkx
assert networkx.__version__ == '3.6.1'
vault = Path(sys.argv[1])
embedder = MeaningFixture()
runtime = register_runtime(SemanticRuntime(vault, embedder))
try:
    value = Organization(Gateway(vault)).snapshot(rebuild=True)
    assert embedder.calls == 0
    print(json.dumps(value, sort_keys=True))
finally:
    runtime.close()
"""
        # One process uses sorted disk feature-cache keys; the other starts
        # without derived organization files and uses original token order.
        for seed, cold in (("1", False), ("917", True)):
            if cold:
                shutil.rmtree(store.vault / ".synapse" / "organization")
            result = subprocess.run(
                [sys.executable, "-c", script, str(store.vault)],
                cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": seed},
                text=True, capture_output=True, timeout=20, check=True,
            )
            assert json.loads(result.stdout) == expected
        assert embedder.calls == 1
        assert _retained_bytes(store) == retained
    finally:
        runtime.close()


def test_explicit_fallback_read_cannot_replace_current_vector_projection(tmp_path):
    rows = [
        _legacy(f"item{i}", name=f"Item {i}", body=text, entity_type="insight")
        for i, text in enumerate(("Growing herbs", "Cultivating basil", "Tending rosemary"))
    ]
    store = _commit(tmp_path / "vault", rows)
    retained = _retained_bytes(store)
    fallback = Organization(Gateway(store.vault), config={"use_local_vectors": True}).snapshot()
    assert fallback["semantic"]["state"] == "unavailable"
    assert fallback["areas"] == []
    embedder = MeaningFixture()
    assert build_index(store.vault, embedder=embedder)["semantic"] == "ok"
    runtime = register_runtime(SemanticRuntime(store.vault, embedder))
    try:
        current = Organization(Gateway(store.vault)).snapshot()
        assert current["semantic"]["state"] == "ready"
        assert len(current["areas"]) == 1
        assert current["organization_revision"] != fallback["organization_revision"]
        historical = Organization(Gateway(store.vault)).snapshot(
            organization_revision=fallback["organization_revision"]
        )
        assert historical == fallback
        next_current = Organization(Gateway(store.vault)).snapshot()
        assert _retained_bytes(store) == retained
        assert next_current == current, (
            "Reading an explicit fallback snapshot poisoned the next implicit current read",
            next_current["semantic"], current["semantic"],
        )
    finally:
        runtime.close()


def test_source_chunks_cover_malformed_long_unicode_and_trailing_whitespace(tmp_path):
    # Deliberately malformed JSON is valid retained text; serialization cannot
    # cause a source paragraph or any non-whitespace character to disappear.
    text = (
        '\ufeff {"body":"unfinished\\\"quote", broken:[\x00\n\n'
        + "漢字🙂e\u0301" * 700
        + "\r\n \t\r\n"
        + "\t  café\u00a0कहानी\u2028終わり\n"
        + "\t" * 12000
        + "last paragraph survives\r\n\t\u2003"
    )
    chunks = _text_chunks(text)
    covered_until = 0
    for start, end, piece in chunks:
        assert 0 <= covered_until <= start < end <= len(text)
        assert 0 < len(piece) <= 900
        assert text[start:end] == piece
        assert not text[covered_until:start].strip()
        covered_until = end
    assert not text[covered_until:].strip()
    assert chunks[-1][2] == "last paragraph survives"
    source = prepare_source(text.encode(), origin="malformed-synthetic.txt")
    unreadable = prepare_source(
        b"bad utf8 \xff\xfe", origin="undecodable.bin", media_type="application/octet-stream",
        extraction={"method": "synthetic", "version": "1", "completeness": "failed"},
    )
    store = _commit(tmp_path / "vault", [], sources=[source, unreadable])
    retained = _retained_bytes(store)
    gateway = Gateway(store.vault)
    passages = source_passages(gateway.view)
    assert len(passages) == len(chunks)
    for passage, (start, end, piece) in zip(passages, chunks, strict=True):
        reference = passage["evidence"]
        assert reference["byte_start"] == len(text[:start].encode())
        assert reference["byte_end"] == len(text[:end].encode())
        assert reference["excerpt_hash"] == hash_bytes(piece.encode())
        assert read_passage(source[0], reference, store.read_object)["excerpt"] == piece
    snapshot = Organization(gateway).snapshot()
    assert snapshot["coverage"]["unreadable_sources"] == 1
    for member in snapshot["members"]:
        assert member["kind"] == "source"
        assert member["qualification"]["assertion"] is False
        assert member["qualification"]["availability"] == "source-material"
    assert _retained_bytes(store) == retained


def test_navigation_and_supporting_source_do_not_satisfy_material_minimum(tmp_path):
    phrase = "harbor sailing tides ropes mooring winds"
    source = prepare_source(phrase.encode(), origin="harbor.txt")
    anchor = evidence_ref(source[0], source[1].__getitem__, 0, len(phrase.encode()))
    example = json.loads((ROOT / "docs/v2/contracts/example-knowledge_record.json").read_text())["payload"]

    def record(kind):
        identity = generate_ulid()
        value = {
            **example, "id": identity, "subject_id": "me", "claim_key": identity,
            "record_kind": kind, "availability": "suggestion", "evidence": [anchor],
            "context_refs": [], "dependencies": [], "statement": phrase,
            "conditions_and_limits": "This saved episode only.", "support": phrase,
            "counterevidence": [], "alternatives": [], "would_change_with": [],
        }
        raw = encode_record(value, name=phrase)
        return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw

    # Distinct claim keys keep both material records eligible in this count check.
    first, second, hint = record("question"), record("question"), record("navigation")
    store = _commit(tmp_path / "vault", [first, second, hint], sources=[source])
    retained = _retained_bytes(store)
    before = Organization(Gateway(store.vault)).snapshot()
    assert before["coverage"]["input_records"] == 2
    assert before["coverage"]["navigation_hints"] == 1
    assert before["areas"] == []
    assert _retained_bytes(store) == retained

    third = record("question")
    store.transact(
        operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
        payload_hash=hash_bytes(b"third-material-record"),
        objects={third[0]["version"]: third[1]},
        mutate=lambda manifest, _read: manifest["records"].update({third[0]["id"]: third[0]}),
    )
    retained = _retained_bytes(store)
    after = Organization(Gateway(store.vault)).snapshot()
    assert after["areas"]
    _assert_references_and_counts(after)
    assert all(area["coverage"]["unique_records"] == 3 for area in after["areas"])
    hint_member = next(member for member in after["members"] if member["id"] == f"record:{hint[0]['id']}")
    assert hint_member["qualification"]["availability"] == "suggestion"
    assert hint_member["qualification"]["record_kind"] == "navigation"
    assert _retained_bytes(store) == retained
