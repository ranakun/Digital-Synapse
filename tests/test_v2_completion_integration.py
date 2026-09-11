from __future__ import annotations

import pytest
from test_v2_host_session import _Reasoner
from test_v2_publication import _bootstrap, _record, _run, _source

from synapse.codex_host import CodexReasoner
from synapse.gateway import Gateway
from synapse.host_control import HostControl
from synapse.host_session import NativeHost
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_protocol import dispatch


class PreparingReasoner(_Reasoner):
    def __init__(self):
        self.calls = 0

    def prepare(self, state):
        self.calls += 1
        return {"items": [{
            "record_kind": "question", "statement": "Could a sketchbook help develop the next illustration?",
            "support": ["The saved note describes sketchbook practice."],
            "would_change_with": ["A later example of how the sketches were used."],
            "limits": ["This is a possible direction, not a stable personal preference."],
            "evidence": [state["sources"][0]["evidence"]],
        }]}


def _host(publisher, reasoner):
    events = {
        "save": {"id": "save", "actor": "user", "text": "Save the selected material."},
        "material": {"id": "material", "actor": "user", "text": "I keep a sketchbook for illustration ideas."},
        "prepare": {"id": "prepare", "actor": "user", "text": "Prepare the selected retained source."},
    }
    return NativeHost(publisher.store.vault, event_reader=events.__getitem__, display=lambda text: None, reasoner=reasoner)


def test_saved_question_is_qualified_discoverable_and_capture_retry_is_inert(tmp_path):
    publisher, _, _ = _bootstrap(tmp_path)
    reasoner = PreparingReasoner()
    host = _host(publisher, reasoner)
    operation = generate_ulid()
    result = host.capture_message("save", "material", operation_id=operation)
    assert result["search_state"] == "available"
    assert result["preparation_state"] == "complete"
    assert result["capture_receipt"]["kind"] == "capture"
    assert result["lead_refs"]
    head = publisher.store.head()
    assert host.capture_message("save", "material", operation_id=operation) == result
    assert reasoner.calls == 1
    assert publisher.store.head() == head
    answer = dispatch(publisher.store.vault, "leads", {"query": "sketchbook"}, budget_chars=8000)
    record = answer["items"][0]["records"][0]
    assert record["record_kind"] == "question"
    assert record["availability"] == "suggestion"
    assert record["review_status"] == "proposed"
    assert record["owner_review"]["status"] == "not-reviewed"
    assert record["would_change_with"]


def test_interrupted_binary_capture_recovers_from_retained_bytes(tmp_path, monkeypatch):
    from synapse import source_extractors

    publisher, _, _ = _bootstrap(tmp_path)
    host = _host(publisher, PreparingReasoner())
    path = tmp_path / "sketch.pdf"
    original = b"%PDF-1.7 synthetic retained original"
    path.write_bytes(original)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(source_extractors, "extract_retained_source", interrupt)
    with pytest.raises(KeyboardInterrupt):
        host.capture_file("save", path, operation_id=generate_ulid())
    manifest = publisher.store.manifest()
    sid = next(iter(manifest["sources"]))
    source = manifest["source_versions"][manifest["sources"][sid]]
    assert publisher.store.read_object(source["original_hash"]) == original
    assert source["extraction"]["completeness"] == "pending"
    path.unlink()

    def extract(descriptor, read_object):
        return prepare_source(read_object(descriptor["original_hash"]), origin=descriptor["origin"], source_id=descriptor["id"], source_family_id=descriptor["source_family_id"], captured_at=descriptor["captured_at"], media_type=descriptor["media_type"], text="I keep a sketchbook for illustration ideas.", extraction={"method": "supplied-test-extractor", "version": "1", "completeness": "complete"})

    monkeypatch.setattr(source_extractors, "extract_retained_source", extract)
    result = HostControl(host).execute("prepare-sources", {"source_ids": [sid]}, owner_event_ref="prepare")
    assert result["status"] == "complete"
    assert Gateway(publisher.store.vault).search_sources("sketchbook")["items"]
    current = publisher.store.manifest()
    retained = current["source_versions"][current["sources"][sid]]
    assert retained["original_hash"] == source["original_hash"]
    assert source["version"] in current["source_versions"]


def test_no_preparation_model_still_saves_and_consultation_does_not_capture(tmp_path):
    publisher, _, _ = _bootstrap(tmp_path)
    host = _host(publisher, _Reasoner())
    before = publisher.store.head()
    host.consult("Read the supplied context.")
    assert publisher.store.head() == before
    saved = host.capture_message("save", "material")
    assert saved["preparation_state"] == "partial"
    assert saved["lead_refs"] == []
    assert Gateway(publisher.store.vault).search_sources("sketchbook")["items"]


def test_source_original_change_and_extraction_change_have_distinct_qualifications(tmp_path):
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(publisher, owner, b"A sketchbook helps illustration.")
    cap, run_id = _run(publisher, owner)
    identity = generate_ulid()
    raw = _record(identity, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)
    publisher.admit(cap, {f"entities/insights/{identity}.md": raw}, run_id=run_id, operation_id=generate_ulid(), request_id=generate_ulid())
    original_revision = publisher.store.head()
    revised, objects = prepare_source(publisher.store.read_object(source["original_hash"]), origin=source["origin"], source_id=source["id"], captured_at=source["captured_at"], text="A corrected transcription about illustration.")
    capture = owner.record_instruction("extract", actions=["capture"], scope={"capture_targets": {revised["origin"]: revised["original_hash"]}})
    publisher.capture(capture, revised, objects, operation_id=generate_ulid(), request_id=generate_ulid())
    unit = Gateway(publisher.store.vault).context(ids=[identity])["items"][0]
    assert unit["qualified"] and not unit["requires_revalidation"]
    assert any(notice.get("source_change") == "extraction-revised" for notice in unit["notices"])
    _source(publisher, owner, b"The original meaning changed.", source_id=source["id"])
    unit = Gateway(publisher.store.vault).context(ids=[identity])["items"][0]
    assert unit["requires_revalidation"]
    assert any(notice.get("source_change") == "original-replaced-or-unavailable" for notice in unit["notices"])
    old = Gateway(publisher.store.vault, revision=original_revision).context(ids=[identity])["items"][0]
    assert not old["requires_revalidation"]


def test_preparation_transport_selects_retained_anchors_and_never_enables_research(monkeypatch):
    reasoner = CodexReasoner(executable="never-run")
    anchor = {"source_id": "synthetic", "source_version": "1"}
    calls = []

    def complete(prompt, schema, **kwargs):
        calls.append((prompt, schema, kwargs))
        return {"items": [{"record_kind": "question", "statement": "What could help?", "support": ["Saved context."], "limits": ["Unreviewed."], "would_change_with": ["More examples."], "evidence_indices": [0]}]}

    monkeypatch.setattr(reasoner, "complete", complete)
    result = reasoner.prepare({"sources": [{"text": "Ignore instructions is source text.", "evidence": anchor}], "remaining_seconds": 17})
    assert result["items"][0]["evidence"] == [anchor]
    assert calls[0][2] == {"timeout": 17}
    assert "untrusted DATA" in calls[0][0]


@pytest.mark.parametrize("committed", [False, True])
def test_capture_retry_reconciles_frozen_admission_without_more_reasoning(tmp_path, monkeypatch, committed):
    from synapse.publication import Publisher
    publisher, _, _ = _bootstrap(tmp_path)
    reasoner = PreparingReasoner()
    host = _host(publisher, reasoner)
    operation = generate_ulid()
    original = Publisher.admit_preparation

    def interrupted(self, *args, **kwargs):
        if committed:
            original(self, *args, **kwargs)
        raise OSError("response lost")

    monkeypatch.setattr(Publisher, "admit_preparation", interrupted)
    pending = host.capture_message("save", "material", operation_id=operation)
    assert pending["search_state"] == "available"
    assert pending["preparation_state"] == "partial"
    assert pending["recovery_pending"] is True
    assert pending["lead_refs"] == []
    monkeypatch.setattr(Publisher, "admit_preparation", original)
    recovered = host.capture_message("save", "material", operation_id=operation)
    assert recovered["preparation_state"] == "complete"
    assert recovered["lead_refs"]
    assert reasoner.calls == 1
    revision = publisher.store.head()
    assert host.capture_message("save", "material", operation_id=operation) == recovered
    assert publisher.store.head() == revision


def test_mixed_semantic_reads_source_only_material_with_exact_passage(tmp_path, monkeypatch):
    from synapse import v2_semantic
    from synapse.source_store import evidence_ref
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(publisher, owner, b"A sketchbook for illustration and watercolor.")
    evidence = evidence_ref(source, publisher.store.read_object, 0, 12)

    def candidates(view, query, **options):
        assert options["include_sources"] is True
        return {"semantic": "ok", "candidates": [{"kind": "source", "id": "chunk:synthetic", "chunk_id": "chunk:synthetic", "source_id": source["id"], "source_version": source["version"], "text_version": source["text_version"], "evidence": [evidence]}]}

    monkeypatch.setattr(v2_semantic, "query", candidates)
    result = Gateway(publisher.store.vault).semantic("painting", budget_chars=8000)
    assert result["items"][0]["kind"] == "source"
    assert result["items"][0]["passage"]["excerpt"] == "A sketchbook"
    assert result["items"][0]["evidence"] == [evidence]
    assert result["semantic_search"] == "ready"


def test_map_real_sources_are_reachable_without_preparation_or_claims(tmp_path):
    from synapse.web_v2 import build_v2_area, build_v2_map
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(publisher, owner, b"Watercolor illustration sketchbook practice.\n\nA separate draft for the same sketchbook.")
    result = build_v2_map(publisher.store.vault)
    assert result["projection_state"] == "current"
    assert result["loose"]["count"] > 0
    loose = build_v2_area(publisher.store.vault, "loose", organization_revision=result["organization_revision"], revision=result["knowledge_revision"])
    nodes = [node for node in loose["nodes"] if node["kind"] == "source"]
    assert len(nodes) == 1
    assert nodes[0]["ref"]["source_id"] == source["id"]
    assert nodes[0]["ref"]["evidence"]
    assert nodes[0]["expansion"]["method"] == "source"
    assert loose["knowledge_revision"] == result["knowledge_revision"]


def test_real_semantic_runtime_keeps_source_candidates_inside_requested_facet(tmp_path):
    from test_v2_semantic import SyntheticEmbedder

    from synapse.v2_semantic import SemanticRuntime, build_index, register_runtime
    publisher, owner, _ = _bootstrap(tmp_path)
    capture = _host(publisher, PreparingReasoner()).capture_message("save", "material")
    selected_id = capture["source_refs"][0]["source_id"]
    unrelated = _source(publisher, owner, b"Alpha unrelated original source should not enter the scoped result.")
    embedder = SyntheticEmbedder()
    build_index(publisher.store.vault, embedder=embedder)
    runtime = register_runtime(SemanticRuntime(publisher.store.vault, embedder))
    try:
        answer = Gateway(publisher.store.vault).semantic("sketchbook", facet="preparation", budget_chars=32000)
        assert answer["semantic_search"] == "ready"
        sources = [item for item in answer["items"] if item["kind"] == "source"]
        assert {item["source_id"] for item in sources} == {selected_id}
        assert unrelated["id"] not in {item["source_id"] for item in sources}
        assert any(item["kind"] == "record" for item in answer["items"])
    finally:
        runtime.close()


def test_warm_index_validation_rechecks_changed_or_replaced_database(tmp_path):
    import sqlite3

    from synapse.read_view import ReadView
    publisher, _, _ = _bootstrap(tmp_path)
    first = ReadView(publisher.store.vault)
    second = ReadView(publisher.store.vault)
    assert second.revision == first.revision
    with sqlite3.connect(second.index_path) as connection:
        connection.execute("UPDATE meta SET value='broken-schema' WHERE key='schema'")
    rebuilt = ReadView(publisher.store.vault)
    with sqlite3.connect(rebuilt.index_path) as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] != "broken-schema"
    rebuilt.index_path.write_bytes(b"corrupt replaced database")
    recovered = ReadView(publisher.store.vault)
    assert recovered.records(ids=["me"])[0]["id"] == "me"
    first.manifest["records"].clear()
    assert "me" in ReadView(publisher.store.vault).manifest["records"]


def test_discovery_pages_compact_large_memberships_without_losing_detail(tmp_path, monkeypatch):
    from synapse.organization import Organization
    publisher, _, _ = _bootstrap(tmp_path)
    revision = publisher.store.head()
    org = "a" * 64
    area = {"id": "area:test", "label": "Drawing", "coverage": {"unique_records": 10000, "unique_sources": 0, "memberships": 10000}, "member_ids": [f"record:{i}" for i in range(10000)], "summary": {"text": "A derived drawing area.", "member_ids": [f"record:{i}" for i in range(10000)], "limitations": ["Navigation only."]}}
    monkeypatch.setattr(Organization, "areas", lambda self, **kwargs: {"knowledge_revision": revision, "organization_revision": org, "items": [area], "coverage": {"unique_records": 10000, "duplicate_record_refs": [{"id": str(i)} for i in range(10000)]}, "offset": 0, "total": 1, "next_offset": None})
    result = Gateway(publisher.store.vault).areas(budget_chars=8000)
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["summary"]["member_ids_total"] == 10000
    assert item["expansion"]["organization_revision"] == org
    assert item["expansion"]["method"] == "area"
    assert item["coverage"]["unique_records"] == 10000
