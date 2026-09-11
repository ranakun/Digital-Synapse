"""Tests for the shared MCP retrieval runtime."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from synapse.embeddings import embed_entities
from synapse.index import connect, reindex
from synapse.retrieval_runtime import EmbeddingWorker, ExactVectorIndex


def _fake_worker(
    requests: Any,
    responses: Any,
    _model: str,
    _threads: int,
) -> None:
    responses.put(("ready", None))
    while True:
        request = requests.get()
        if request is None:
            return
        request_id, texts = request
        if texts == ["hang"]:
            time.sleep(10)
            continue
        time.sleep(0.04)
        responses.put(
            (
                "result",
                request_id,
                [[float(len(text)), 1.0] for text in texts],
            )
        )


class _DirectionalEmbedder:
    model = "directional-test"

    def __init__(self, *, flipped: bool = False) -> None:
        self.flipped = flipped

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            is_alice = "Alice" in text
            alice_vector = [0.0, 1.0] if self.flipped else [1.0, 0.0]
            bob_vector = [1.0, 0.0] if self.flipped else [0.0, 1.0]
            vectors.append(alice_vector if is_alice else bob_vector)
        return vectors


@pytest.fixture()
def vector_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    people = vault / "entities" / "people"
    people.mkdir(parents=True)
    (people / "alice.md").write_text(
        "---\nid: alice\ntype: person\nname: Alice\nreview_status: proposed\n---\n",
        encoding="utf-8",
    )
    (people / "bob.md").write_text(
        "---\nid: bob\ntype: person\nname: Bob\nreview_status: proposed\n---\n",
        encoding="utf-8",
    )
    reindex(vault, full=True)
    embed_entities(vault, embedder=_DirectionalEmbedder(), force_all=True)
    return vault


def test_exact_vector_index_matches_cosine_and_type_filter(
    vector_vault: Path,
) -> None:
    index = ExactVectorIndex(vector_vault)
    try:
        index.preload()
        matches = index.search(
            [1.0, 0.0],
            model="directional-test",
            entity_type="person",
            limit=2,
        )
        assert [match["row"]["id"] for match in matches] == ["alice", "bob"]
        assert matches[0]["score"] == pytest.approx(1.0)
        assert index.search(
            [1.0, 0.0],
            model="directional-test",
            entity_type="company",
            limit=2,
        ) == []
    finally:
        index.close()


def test_exact_vector_index_reloads_after_external_embedding_commit(
    vector_vault: Path,
) -> None:
    index = ExactVectorIndex(vector_vault)
    try:
        index.preload()
        before = index.search(
            [1.0, 0.0],
            model="directional-test",
            entity_type=None,
            limit=1,
        )
        assert before[0]["row"]["id"] == "alice"

        embed_entities(
            vector_vault,
            embedder=_DirectionalEmbedder(flipped=True),
            force_all=True,
        )
        after = index.search(
            [1.0, 0.0],
            model="directional-test",
            entity_type=None,
            limit=1,
        )
        assert after[0]["row"]["id"] == "bob"
    finally:
        index.close()


def test_exact_vector_index_rejects_malformed_snapshot(vector_vault: Path) -> None:
    index = ExactVectorIndex(vector_vault)
    conn = connect(vector_vault)
    try:
        with conn:
            conn.execute("UPDATE embeddings SET dim = 3 WHERE entity_id = 'alice'")
        with pytest.raises(ValueError, match="mixed models or dimensions"):
            index.preload()
    finally:
        conn.close()
        index.close()


def test_embedding_worker_serializes_three_concurrent_callers() -> None:
    worker = EmbeddingWorker(
        "fake",
        request_timeout=1.0,
        startup_timeout=3.0,
        worker_target=_fake_worker,
    )
    try:
        worker.start()
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(worker.embed, [f"q-{index}"]) for index in range(3)]
            results = [future.result(timeout=2) for future in futures]
        assert results == [[[3.0, 1.0]], [[3.0, 1.0]], [[3.0, 1.0]]]
    finally:
        worker.close()
    assert worker.pid is None


def test_embedding_worker_timeout_is_bounded_and_recovers() -> None:
    worker = EmbeddingWorker(
        "fake",
        request_timeout=0.5,
        startup_timeout=3.0,
        worker_target=_fake_worker,
    )
    try:
        worker.start()
        old_pid = worker.pid
        started = time.perf_counter()
        with pytest.raises(TimeoutError, match="inference timed out"):
            worker.embed(["hang"])
        assert time.perf_counter() - started < 1.0
        assert worker.pid is not None
        assert worker.pid != old_pid

        worker.start()
        assert worker.embed(["healthy"]) == [[7.0, 1.0]]
    finally:
        worker.close()


def test_embedding_worker_queue_wait_uses_same_absolute_deadline() -> None:
    worker = EmbeddingWorker(
        "fake",
        request_timeout=0.5,
        startup_timeout=3.0,
        worker_target=_fake_worker,
    )
    barrier = threading.Barrier(3)

    def call(text: str) -> str:
        barrier.wait()
        try:
            worker.embed([text])
        except TimeoutError:
            return "timeout"
        return "ok"

    try:
        worker.start()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as executor:
            outcomes = list(executor.map(call, ["hang", "queued-a", "queued-b"]))
        elapsed = time.perf_counter() - started
        assert "timeout" in outcomes
        assert elapsed < 1.2
    finally:
        worker.close()
