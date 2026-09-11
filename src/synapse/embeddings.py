"""Local embedding support for duplicate detection and candidate recall."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Protocol

from synapse.config import load_config
from synapse.index import connect, ensure_schema, reindex
from synapse.util import blob_to_vector, hash_embedding, vector_to_blob

_MANAGED_SECTION_RE = re.compile(
    r'<!--\s*synapse:managed\s*-->.*?<!--\s*/synapse:managed\s*-->',
    re.DOTALL,
)


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    model = "hash-local-test"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]


class FastEmbedder:
    def __init__(
        self,
        model: str,
        *,
        threads: int | None = None,
        local_files_only: bool = False,
    ) -> None:
        self.model = model
        try:
            from fastembed import TextEmbedding
        except Exception as exc:
            raise RuntimeError("fastembed is not installed; install the embeddings extra") from exc
        self._model = TextEmbedding(
            model_name=model,
            threads=threads,
            local_files_only=local_files_only,
            **({"cache_dir": os.environ["SYNAPSE_MODEL_CACHE"]} if os.environ.get("SYNAPSE_MODEL_CACHE") else {}),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Cap batch_size: fastembed's default (256) builds an O(seq_len^2)
        # attention buffer per batch that can demand multiple GB and crash
        # onnxruntime with a RUNTIME_EXCEPTION on constrained machines. A small
        # batch keeps peak allocation bounded at a negligible throughput cost.
        return [list(vector) for vector in self._model.embed(texts, batch_size=32)]


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


def embedding_text(row: dict) -> str:  # type: ignore[type-arg]
    """Compose the text to embed for an entity row dict.

    The row dict may have keys: id, type, name, body, frontmatter (JSON string).
    Tags are extracted from the frontmatter JSON.
    Managed-section HTML comments are stripped from the body.
    """
    type_ = row.get("type", "")
    name = row.get("name", "")
    body = row.get("body") or ""
    body = _MANAGED_SECTION_RE.sub("", body).strip()

    # Extract tags from frontmatter JSON if present
    frontmatter_raw = row.get("frontmatter")
    tags: list[str] = []
    if frontmatter_raw:
        try:
            fm = json.loads(frontmatter_raw)
            raw_tags = fm.get("tags") or []
            tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        except (json.JSONDecodeError, AttributeError):
            tags = []
    # Also support tags passed directly in the row dict
    if not tags and row.get("tags"):
        raw = row["tags"]
        tags = list(raw) if isinstance(raw, list) else []

    tag_str = ", ".join(tags) if tags else ""
    prefix = f"{type_} | tags: {tag_str}" if tag_str else type_
    return f"{prefix}\n{name}\n{body}"


def stored_models(conn) -> set[str]:  # type: ignore[type-arg]
    """Return the set of distinct model values in the embeddings table."""
    rows = conn.execute("SELECT DISTINCT model FROM embeddings").fetchall()
    return {row[0] for row in rows if row[0]}


def maybe_suggest_embed(changed_count: int) -> None:
    if changed_count > 25:
        print("Tip: run `synapse embed` to update embeddings for changed entities.", flush=True)


def embed_entities(
    vault: str | Path | None = None,
    *,
    embedder: Embedder | None = None,
    force_all: bool = False,
) -> dict[str, int | str]:
    reindex(vault)
    conn = connect(vault)
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, type, name, body, frontmatter FROM entities ORDER BY id"
        ).fetchall()
        emb = embedder or default_embedder(vault)
        model = getattr(emb, "model", emb.__class__.__name__)

        # Build lookup: entity_id -> (stored_model, stored_text_hash)
        stored: dict[str, tuple[str, str | None]] = {}
        for stored_row in conn.execute(
            "SELECT entity_id, model, text_hash FROM embeddings"
        ).fetchall():
            stored[stored_row[0]] = (stored_row[1], stored_row[2])

        # Determine which rows need embedding
        to_embed = []
        skipped = 0
        for row in rows:
            row_dict = dict(row)
            text = embedding_text(row_dict)
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            if not force_all and row_dict["id"] in stored:
                stored_model, stored_hash = stored[row_dict["id"]]
                if stored_model == model and stored_hash == text_hash:
                    skipped += 1
                    continue
            to_embed.append((row_dict, text, text_hash))

        # Embed only the rows that need it
        texts = [item[1] for item in to_embed]
        vectors = emb.embed(texts) if texts else []

        with conn:
            for (row_dict, _text, text_hash), vector in zip(to_embed, vectors, strict=False):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings(entity_id, model, dim, vector, text_hash)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (row_dict["id"], model, len(vector), vector_to_blob(vector), text_hash),
                )
            # Drop embeddings whose entity no longer exists (FIX-11).
            conn.execute(
                "DELETE FROM embeddings WHERE entity_id NOT IN (SELECT id FROM entities)"
            )

        embedded = min(len(to_embed), len(vectors))
        total = len(rows)
        return {"embedded": embedded, "skipped": skipped, "total": total, "model": model}
    finally:
        conn.close()


def nearest_duplicates(
    vault: str | Path | None = None, *, threshold: float = 0.95
) -> list[dict[str, object]]:
    conn = connect(vault)
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.name, e.type, emb.vector FROM embeddings emb
            JOIN entities e ON e.id = emb.entity_id
            ORDER BY e.name
            """
        ).fetchall()
        vector_groups = {}
        for row in rows:
            if row["type"] == "skill":
                continue
            vector = blob_to_vector(row["vector"])
            vector_groups.setdefault((row["type"], len(vector)), []).append(
                (row["id"], row["name"], vector)
            )
        matches = []
        try:
            import numpy as np
        except ImportError:
            np = None

        if np is not None:
            for vectors in vector_groups.values():
                matrix = np.asarray([item[2] for item in vectors], dtype=np.float64)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                normalized = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)
                # ponytail: exact O(n²) scan in bounded chunks; use ANN only if vault
                # growth makes compiled matrix multiplication measurably slow again.
                for start in range(0, len(vectors), 256):
                    stop = min(start + 256, len(vectors))
                    scores = normalized[start:stop] @ normalized.T
                    for offset, left_index in enumerate(range(start, stop)):
                        right_indices = np.flatnonzero(
                            scores[offset, left_index + 1 :] >= threshold
                        ) + left_index + 1
                        for right_index in right_indices:
                            left = vectors[left_index]
                            right = vectors[int(right_index)]
                            matches.append(
                                {
                                    "a": left[0],
                                    "b": right[0],
                                    "a_name": left[1],
                                    "b_name": right[1],
                                    "score": float(scores[offset, right_index]),
                                }
                            )
            return sorted(matches, key=lambda item: item["score"], reverse=True)

        for vectors in vector_groups.values():
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
