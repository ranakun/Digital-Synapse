from __future__ import annotations

import shutil
import sqlite3

from test_v2_read_view import _commit, _legacy

from synapse.gateway import Gateway
from synapse.organization import Organization
from synapse.v2_semantic import SemanticRuntime, build_index, query, register_runtime


class MeaningFixture:
    model = "synthetic-organization-meaning-v1"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        values = []
        for text in texts:
            if "bridge" in text:
                values.append([0.8, 0.6, 0.0])
            elif any(word in text for word in ("herbs", "basil", "rosemary")):
                values.append([1.0, 0.0, 0.0])
            elif any(word in text for word in ("melodies", "arpeggios", "sonatas")):
                values.append([0.3, 0.953939, 0.0])
            else:
                values.append([0.0, 0.0, 1.0])
        return values


def _meaning_vault(tmp_path):
    texts = ["Growing herbs", "Cultivating basil", "Tending rosemary", "Improvising melodies", "Practising arpeggios", "Playing sonatas", "bridge", "Repairing bicycles"]
    rows = [_legacy(f"item{i}", name=f"Item {i}", body=text, entity_type="insight") for i, text in enumerate(texts)]
    store = _commit(tmp_path / "vault", rows)
    embedder = MeaningFixture()
    report = build_index(store.vault, embedder=embedder)
    runtime = register_runtime(SemanticRuntime(store.vault, embedder))
    return store, embedder, runtime, report


def test_local_vectors_recover_paraphrases_overlap_and_rebuild_without_model_calls(tmp_path):
    store, embedder, runtime, _ = _meaning_vault(tmp_path)
    try:
        calls = embedder.calls
        first = Organization(Gateway(store.vault)).snapshot(rebuild=True)
        assert first["method"] == "lexical-tfidf+local-vector"
        assert first["semantic"]["input_hash"]
        assert any({"record:item0", "record:item1", "record:item2"} <= set(area["member_ids"]) for area in first["areas"])
        assert any({"record:item3", "record:item4", "record:item5"} <= set(area["member_ids"]) for area in first["areas"])
        bridge = next(member for member in first["members"] if member["id"] == "record:item6")
        assert len(bridge["area_ids"]) >= 2
        assert "record:item7" in first["loose"]
        assert embedder.calls == calls
        shutil.rmtree(store.vault / ".synapse" / "organization")
        cold = Organization(Gateway(store.vault)).snapshot(rebuild=True)
        assert cold == first
        assert embedder.calls == calls
    finally:
        runtime.close()


def test_record_vector_reference_cannot_override_candidate_identity(tmp_path):
    store, _, runtime, report = _meaning_vault(tmp_path)
    try:
        with sqlite3.connect(report["path"]) as connection:
            connection.execute("UPDATE vectors SET ref_json=? WHERE identity=?", ('{"id":"forged","kind":"source"}', "item0"))
        result = query(Gateway(store.vault).view, "herbs", include_sources=True)
        assert result["semantic"] != "ok"
        assert result["candidates"] == []
    finally:
        runtime.close()


def test_source_chunking_preserves_final_paragraph_and_exact_unicode_offsets():
    from synapse.v2_semantic import _text_chunks
    text = "First paragraph.\n\n  Important final café 😀 paragraph.\n"
    chunks = _text_chunks(text)
    assert [chunk[2] for chunk in chunks] == ["First paragraph.", "Important final café 😀 paragraph."]
    for start, end, content in chunks:
        assert text[start:end] == content
    # Long paragraphs ending in whitespace used to trigger repeated full
    # regex scans. This case also verifies every non-whitespace character.
    long_text = "Opening.\n\n" + "observations café 😀 " * 30000 + "final detail.\n"
    chunks = _text_chunks(long_text)
    assert all(len(content) <= 900 for _, _, content in chunks)
    assert "".join("".join(content.split()) for _, _, content in chunks) == "".join(long_text.split())


def test_navigation_features_ignore_serialization_keys_but_keep_content():
    from synapse.organization import _tokens
    text = '{"id":"opaque-id", "path":"entities/companies/folder.md", "body":"Growing herbs\\u2014basil", "type":"gardening"}'
    assert set(_tokens(text)) == {"growing", "herbs", "basil", "gardening"}
