from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest

from synapse.knowledge import encode_record, record_descriptor
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.v2_semantic import (
    SemanticRuntime,
    build_index,
    query,
    register_runtime,
    source_passages,
)


class SyntheticEmbedder:
    model = "synthetic-semantic-test"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] if "alpha" in text.casefold() else [0.0, 1.0] for text in texts]


class ParaphraseEmbedder(SyntheticEmbedder):
    model = "synthetic-paraphrase-test"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [
            [1.0, 0.0] if {"alpha", "paraphrase"} & set(text.casefold().split()) else [0.0, 1.0]
            for text in texts
        ]


class ShortEmbedder(SyntheticEmbedder):
    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts[:-1]]


class SlowEmbedder(SyntheticEmbedder):
    def __init__(self, released: threading.Event) -> None:
        super().__init__()
        self.released = released

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.released.wait(timeout=30)
        return [[1.0, 0.0] for _ in texts]


def _legacy(identity: str, name: str, body: str, *, subject_id: str | None = None) -> tuple[dict, bytes]:
    properties = {} if subject_id is None else {"subject_id": subject_id, "facets": ["work"]}
    raw = (
        "---\n"
        + f"id: {identity}\n"
        + "type: insight\n"
        + f"name: {name}\n"
        + "review_status: proposed\n"
        + "properties: "
        + json.dumps(properties)
        + "\n---\n\n"
        + body
        + "\n"
    ).encode("utf-8")
    return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw


def _store(tmp_path: Path) -> RevisionStore:
    rows = [
        _legacy("me", "Owner", "alpha owner", subject_id="me"),
        _legacy(generate_ulid(), "Alpha", "alpha record", subject_id="me"),
        _legacy(generate_ulid(), "Beta", "beta record", subject_id="other"),
    ]
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    objects = {raw_row[0]["version"]: raw_row[1] for raw_row in rows}
    descriptors = {raw_row[0]["id"]: raw_row[0] for raw_row in rows}
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"semantic-baseline"),
        objects=objects,
        initialize=True,
        mutate=lambda manifest, _read: manifest["records"].update(descriptors),
    )
    return store


def _v2_record(
    identity: str,
    *,
    name: str,
    availability: str = "accepted",
    disposition: str = "none",
    context_refs: list[dict] | None = None,
    evidence: list[dict] | None = None,
) -> tuple[dict, bytes]:
    payload = json.loads(
        (Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json").read_text()
    )["payload"]
    payload.update(
        id=identity,
        subject_id="me",
        claim_key=name.casefold(),
        record_kind="question",
        availability=availability,
        evidence=evidence or [],
        context_refs=context_refs or [],
        statement=f"alpha {name}",
    )
    if disposition != "none":
        payload["owner_review"] = {
            "status": "reviewed",
            "disposition": disposition,
            "receipt_id": generate_ulid(),
        }
    raw = encode_record(payload, name=name)
    return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw


def test_semantic_build_query_scopes_before_ranking_and_caches_runtime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder()
    report = build_index(store.vault, embedder=embedder)
    assert report["semantic"] == "ok"
    assert report["knowledge_revision"] == store.head()
    assert Path(report["path"]).is_file()
    assert embedder.calls == 1

    view = ReadView(store.vault, revision=store.head())
    result = query(view, "alpha", subject_id="me", facet="work", limit=1, embedder=embedder)
    assert result["semantic"] == "ok"
    assert result["knowledge_revision"] == view.revision
    assert len(result["candidates"]) == 1
    alpha_id = next(identity for identity, row in store.manifest()["records"].items() if row["name"] == "Alpha")
    assert result["candidates"][0]["id"] == alpha_id
    assert result["candidates"][0]["version"] == store.manifest()["records"][alpha_id]["version"]

    runtime = register_runtime(SemanticRuntime(store.vault, embedder, cache_size=1))
    assert runtime.query("alpha") == runtime.query("alpha")
    calls_after_first = embedder.calls
    runtime.query("beta")
    runtime.query("alpha")
    assert embedder.calls == calls_after_first + 2


def test_semantic_query_refuses_without_prepared_runtime_and_hash_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = ReadView(store.vault)
    missing = query(view, "alpha")
    assert missing["semantic"] == "refused:no-warm-runtime"
    assert missing["fallback"] == {"kind": "lexical", "delegate": "caller"}

    class HashLike(SyntheticEmbedder):
        model = "hash-local-test"

    hash_embedder = HashLike()
    report = build_index(store.vault, embedder=hash_embedder)
    assert report["semantic"] == "refused:hash-fallback"
    assert query(view, "alpha", embedder=hash_embedder)["semantic"] == "refused:hash-fallback"


def test_semantic_index_rejects_short_vectors_and_corruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    short = build_index(store.vault, embedder=ShortEmbedder())
    assert short["semantic"] == "refused:invalid-vector"

    embedder = SyntheticEmbedder()
    report = build_index(store.vault, embedder=embedder)
    view = ReadView(store.vault)
    path = Path(report["path"])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE vectors SET text_hash = ? WHERE identity = 'me'", ("0" * 64,))
        connection.commit()
    result = query(view, "alpha", embedder=embedder)
    assert result["semantic"] == "refused:stale-index"
    assert result["candidates"] == []

    build_index(store.vault, embedder=embedder)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE vectors SET vector = ? WHERE identity = 'me'",
            (struct.pack("ff", math.nan, 0.0),),
        )
        connection.commit()
    result = query(view, "alpha", embedder=embedder)
    assert result["semantic"] == "refused:stale-index"
    assert "vector-invalid" in result["omissions"][0]


def test_semantic_query_requires_matching_model_and_preserves_pinned_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.head()
    first_view = ReadView(store.vault, revision=first)
    embedder = SyntheticEmbedder()
    build_index(store.vault, revision=first, embedder=embedder)

    class OtherModel(SyntheticEmbedder):
        model = "other-semantic-model"

    assert query(first_view, "alpha", embedder=OtherModel())["semantic"] == "refused:semantic-index-model-mismatch"
    assert first_view.revision == first
    assert first_view.manifest["records"]["me"]["version"] == store.manifest(first)["records"]["me"]["version"]


def test_warm_validated_vector_cache_detects_later_index_corruption(tmp_path):
    store = _store(tmp_path)
    embedder = SyntheticEmbedder()
    report = build_index(store.vault, embedder=embedder)
    runtime = SemanticRuntime(store.vault, embedder)
    register_runtime(runtime)
    try:
        view = ReadView(store.vault)
        assert query(view, "alpha")["semantic"] == "ok"
        with sqlite3.connect(report["path"]) as connection:
            connection.execute("UPDATE vectors SET text_hash=? WHERE identity='me'", ("0" * 64,))
        result = query(view, "alpha")
        assert result["semantic"] == "refused:stale-index"
        assert result["candidates"] == []
    finally:
        runtime.close()


def test_semantic_query_bounds_direct_embedder_and_handles_yaml_dates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    date_raw = (
        b"---\nid: date-record\ntype: insight\nname: Date\nreview_status: proposed\n"
        b"created_at: 2026-09-11\nproperties: {}\n---\n\nalpha date\n"
    )
    date_row = record_descriptor(date_raw, path="entities/insights/date-record.md")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"date-record"),
        objects={date_row["version"]: date_raw},
        mutate=lambda manifest, _read: manifest["records"].update({date_row["id"]: date_row}),
    )
    embedder = SyntheticEmbedder()
    report = build_index(store.vault, embedder=embedder)
    assert report["semantic"] == "ok"

    released = threading.Event()
    slow = SlowEmbedder(released)
    view = ReadView(store.vault)
    started = time.monotonic()
    result = query(view, "alpha", embedder=slow)
    elapsed = time.monotonic() - started
    released.set()
    assert result["semantic"] == "refused:timeout"
    assert elapsed < 3.0


def test_semantic_scope_preserves_corrections_and_excludes_unavailable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target_id = generate_ulid()
    target_row, target_raw = _v2_record(target_id, name="Superseded")
    draft_row, draft_raw = _v2_record(generate_ulid(), name="Draft", availability="draft")
    dismissed_row, dismissed_raw = _v2_record(generate_ulid(), name="Dismissed", disposition="dismissed")
    disputed_row, disputed_raw = _v2_record(generate_ulid(), name="Disputed", disposition="disputed")
    declined_row, declined_raw = _v2_record(generate_ulid(), name="Declined", disposition="declined-adoption")
    reviser_row, reviser_raw = _v2_record(
        generate_ulid(),
        name="Reviser",
        context_refs=[{"id": target_id, "version": target_row["version"], "role": "revises", "scope": "updated"}],
    )
    rows = [target_row, draft_row, dismissed_row, disputed_row, declined_row, reviser_row]
    raws = [target_raw, draft_raw, dismissed_raw, disputed_raw, declined_raw, reviser_raw]
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="suggestion-admission",
        payload_hash=hash_bytes(b"semantic-scope-records"),
        objects={row["version"]: raw for row, raw in zip(rows, raws, strict=True)},
        mutate=lambda manifest, _read: manifest["records"].update({row["id"]: row for row in rows}),
    )
    embedder = SyntheticEmbedder()
    assert build_index(store.vault, embedder=embedder)["semantic"] == "ok"
    result = query(ReadView(store.vault), "alpha", embedder=embedder)
    ids = {item["id"] for item in result["candidates"]}
    assert target_id in ids  # a proposed correction cannot supersede accepted knowledge
    assert declined_row["id"] in ids
    assert not ids.intersection({draft_row["id"], dismissed_row["id"], disputed_row["id"]})


def test_semantic_query_applies_read_view_decision_duplicate_suppression_before_cap(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    embedder = SyntheticEmbedder()
    assert build_index(store.vault, embedder=embedder)["semantic"] == "ok"
    view = ReadView(store.vault)
    alpha_id = next(
        identity
        for identity, row in view.manifest["records"].items()
        if row["name"] == "Alpha"
    )
    with sqlite3.connect(view.index_path) as connection:
        connection.execute(
            "INSERT INTO decision_duplicates(record_id,decision_id) VALUES (?, ?)",
            (alpha_id, "dismissed-decision"),
        )
        connection.commit()
    result = query(view, "alpha", subject_id="me", limit=1, embedder=embedder)
    assert result["semantic"] == "ok"
    assert result["candidates"][0]["id"] == "me"
    assert alpha_id not in {item["id"] for item in result["candidates"]}


def test_supplied_local_vector_fixture_recovers_a_nonlexical_paraphrase(tmp_path: Path) -> None:
    store = _store(tmp_path)
    embedder = ParaphraseEmbedder()
    assert build_index(store.vault, embedder=embedder)["semantic"] == "ok"
    result = query(ReadView(store.vault), "paraphrase terminology", embedder=embedder)
    alpha_id = next(identity for identity, row in store.manifest()["records"].items() if row["name"] == "Alpha")
    assert result["semantic"] == "ok"
    assert alpha_id in {item["id"] for item in result["candidates"]}


def test_runtime_validates_cache_size() -> None:
    with pytest.raises(V2Error):
        SemanticRuntime(Path("/tmp/semantic-test"), SyntheticEmbedder(), cache_size=0)


def test_semantic_candidates_include_current_retained_source_passages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    descriptor, objects = prepare_source(
        b"alpha source passage\n\nother source material",
        origin="semantic-source.txt",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"semantic-source"),
        objects=objects,
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
    )
    embedder = SyntheticEmbedder()
    report = build_index(store.vault, embedder=embedder)
    assert report["semantic"] == "ok"
    default_result = query(ReadView(store.vault), "alpha", embedder=embedder)
    assert all(item["kind"] == "record" for item in default_result["candidates"])
    result = query(ReadView(store.vault), "alpha", embedder=embedder, include_sources=True)
    source = next(item for item in result["candidates"] if item["kind"] == "source")
    assert source["source_id"] == descriptor["id"]
    assert source["source_version"] == descriptor["version"]
    assert source["evidence"][0]["text_version"] == descriptor["text_version"]

    scoped = query(
        ReadView(store.vault),
        "alpha",
        subject_id="me",
        include_sources=True,
        embedder=embedder,
    )
    assert all(item["kind"] == "record" for item in scoped["candidates"])
    assert any("source-only material is excluded" in limitation for limitation in scoped["limitations"])

    source_identity = source_passages(ReadView(store.vault))[0]["identity"]
    with sqlite3.connect(Path(report["path"])) as connection:
        connection.execute(
            "UPDATE vectors SET ref_json=? WHERE identity=?",
            (json.dumps({"source_id": descriptor["id"], "source_version": descriptor["version"], "text_version": descriptor["text_version"], "evidence": []}), source_identity),
        )
        connection.commit()
    tampered = query(ReadView(store.vault), "alpha", embedder=embedder, include_sources=True)
    assert tampered["semantic"] == "refused:stale-index"
    assert any("reference-mismatch" in omission for omission in tampered["omissions"])


def test_scoped_source_candidates_require_an_exact_retained_record_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    descriptor, source_objects = prepare_source(
        b"alpha anchored source passage",
        origin="anchored-source.txt",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T08:00:00Z",
    )
    anchored_row, anchored_raw = _v2_record(
        generate_ulid(),
        name="Anchored",
        evidence=[
            {
                "source_id": descriptor["id"],
                "source_version": descriptor["version"],
                "text_version": descriptor["text_version"],
                "byte_start": 0,
                "byte_end": 5,
                "excerpt_hash": hash_bytes(b"alpha"),
                "source_family_id": descriptor["source_family_id"],
            }
        ],
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"anchored-source"),
        objects={**source_objects, anchored_row["version"]: anchored_raw},
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
            manifest["records"].update({anchored_row["id"]: anchored_row}),
        ),
    )
    embedder = SyntheticEmbedder()
    assert build_index(store.vault, embedder=embedder)["semantic"] == "ok"
    result = query(
        ReadView(store.vault),
        "alpha",
        subject_id="me",
        include_sources=True,
        embedder=embedder,
    )
    source = next(item for item in result["candidates"] if item["kind"] == "source")
    assert source["source_id"] == descriptor["id"]
    assert any("anchored" in limitation for limitation in result["limitations"])
