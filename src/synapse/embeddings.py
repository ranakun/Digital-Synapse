"""Local embedding support for duplicate detection and candidate recall."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from synapse.config import load_config
from synapse.index import connect, ensure_schema, reindex
from synapse.util import blob_to_vector, hash_embedding, vector_to_blob


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    model = "hash-local-test"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]


class FastEmbedder:
    def __init__(self, model: str) -> None:
        self.model = model
        try:
            from fastembed import TextEmbedding
        except Exception as exc:
            raise RuntimeError("fastembed is not installed; install the embeddings extra") from exc
        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(vector) for vector in self._model.embed(texts)]


def default_embedder(vault: str | Path | None = None) -> Embedder:
    cfg = load_config(vault)
    if cfg["embeddings"].get("provider") == "fastembed":
        try:
            return FastEmbedder(cfg["embeddings"]["model"])
        except Exception:
            return HashEmbedder()
    return HashEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_entities(
    vault: str | Path | None = None, *, embedder: Embedder | None = None
) -> dict[str, int | str]:
    reindex(vault)
    conn = connect(vault)
    try:
        ensure_schema(conn)
        rows = conn.execute("SELECT id, name, body FROM entities ORDER BY id").fetchall()
        texts = [f"{row['name']}\n{row['body'] or ''}" for row in rows]
        emb = embedder or default_embedder(vault)
        model = getattr(emb, "model", emb.__class__.__name__)
        vectors = emb.embed(texts) if texts else []
        with conn:
            for row, vector in zip(rows, vectors, strict=False):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings(entity_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                    (row["id"], model, len(vector), vector_to_blob(vector)),
                )
        return {"embedded": min(len(rows), len(vectors)), "model": model}
    finally:
        conn.close()


def nearest_duplicates(
    vault: str | Path | None = None, *, threshold: float = 0.86
) -> list[dict[str, object]]:
    conn = connect(vault)
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.name, emb.vector FROM embeddings emb
            JOIN entities e ON e.id = emb.entity_id
            ORDER BY e.name
            """
        ).fetchall()
        vectors = [(row["id"], row["name"], blob_to_vector(row["vector"])) for row in rows]
        matches = []
        for index, left in enumerate(vectors):
            for right in vectors[index + 1 :]:
                score = cosine(left[2], right[2])
                if score >= threshold:
                    matches.append(
                        {
                            "a": left[0],
                            "b": right[0],
                            "a_name": left[1],
                            "b_name": right[1],
                            "score": score,
                        }
                    )
        return sorted(matches, key=lambda item: item["score"], reverse=True)
    finally:
        conn.close()
