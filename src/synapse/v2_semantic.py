"""Explicit, revision-bound semantic candidate retrieval for v2.

This module deliberately does not construct a default embedder.  A caller must
explicitly provide a prepared embedder or register a warm ``SemanticRuntime``.
The SQLite file is disposable; retained Markdown and the revision manifest are
the authority for every vector's identity and text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from synapse.embeddings import cosine, embedding_text
from synapse.knowledge import metadata_and_body
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore
from synapse.source_purpose import validate_source_scope
from synapse.util import blob_to_vector, vector_to_blob
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

_SCHEMA = "v2-semantic/2"
_MAX_CACHE = 256
_QUERY_TIMEOUT_SECONDS = 1.5
_RUNTIME_REGISTRY: dict[Path, SemanticRuntime] = {}


def _refusal(
    revision: str,
    reason: str,
    *,
    omissions: Iterable[str] = (),
) -> dict[str, Any]:
    status = f"refused:{reason}"
    return {
        "candidates": [],
        "semantic": status,
        "semantic_search": status,
        "knowledge_revision": revision,
        "revision": revision,
        "index_state": "unavailable",
        "omissions": list(omissions) or [reason],
        "fallback": {"kind": "lexical", "delegate": "caller"},
    }


def _model(embedder: Any) -> str | None:
    value = getattr(embedder, "model", None)
    return value if isinstance(value, str) and value else None


def _vector(value: Any) -> list[float] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        result.append(number)
    return result if result else None


def _embedding_row(identity: str, version: str, raw: bytes) -> tuple[str, str]:
    metadata, body = metadata_and_body(raw)
    row = {
        "id": identity,
        "type": metadata.get("type", ""),
        "name": metadata.get("name", ""),
        "body": body,
        "frontmatter": json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, default=str
        ),
    }
    text = embedding_text(row)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _index_path(vault: Path, revision: str) -> Path:
    return vault / ".synapse" / "v2-semantic" / f"{revision}.sqlite"


def _source_identity(source_id: str, source_version: str, start: int, end: int) -> str:
    """Return the stable identity of one retained source passage."""

    return "source:" + hash_bytes(
        canonical_json([source_id, source_version, start, end])
    )


def _text_chunks(text: str, *, maximum: int = 900) -> list[tuple[int, int, str]]:
    """Split text into deterministic, bounded, non-empty coherent blocks."""

    if not text.strip():
        return []
    chunks: list[tuple[int, int, str]] = []
    # Scan separators instead of retrying a lazy whole-paragraph expression
    # from every character. A final newline is valid retained text, and must
    # neither discard its paragraph nor trigger quadratic backtracking.
    blocks = []
    block_start = 0
    for separator in re.finditer(r"\n(?:[^\S\n]*\n)+", text):
        blocks.append((block_start, separator.start()))
        block_start = separator.end()
    blocks.append((block_start, len(text)))
    for start, end in blocks:
        cursor = start
        while end - cursor > maximum:
            cut = text.rfind(" ", cursor, cursor + maximum)
            if cut <= cursor:
                cut = cursor + maximum
            piece = text[cursor:cut].strip()
            if piece:
                piece_start = cursor + len(text[cursor:cut]) - len(text[cursor:cut].lstrip())
                chunks.append((piece_start, piece_start + len(piece), piece))
            cursor = cut
            while cursor < end and text[cursor].isspace():
                cursor += 1
        piece = text[cursor:end].strip()
        if piece:
            piece_start = cursor + len(text[cursor:end]) - len(text[cursor:end].lstrip())
            chunks.append((piece_start, piece_start + len(piece), piece))
    return chunks


def source_passages(view: ReadView) -> list[dict[str, Any]]:
    """Enumerate current readable source passages for semantic consumers.

    The returned passages are retrieval units, not canonical records.  A
    missing text version remains represented by the caller's coverage report,
    but cannot be embedded or claimed searchable.
    """

    passages: list[dict[str, Any]] = []
    current = view.manifest.get("sources") or {}
    versions = view.manifest.get("source_versions") or {}
    for source_id, version in sorted(current.items()):
        descriptor = versions.get(version)
        if (
            not isinstance(descriptor, Mapping)
            or str(descriptor.get("id")) != str(source_id)
            or str(descriptor.get("version")) != str(version)
            or not descriptor.get("text_version")
        ):
            continue
        try:
            original = view.store.read_object(str(descriptor["original_hash"]))
            text_bytes = view.store.read_object(str(descriptor["text_version"]))
            if hash_bytes(original) != descriptor["original_hash"] or hash_bytes(text_bytes) != descriptor["text_version"]:
                continue
            text = text_bytes.decode("utf-8")
        except (UnicodeDecodeError, V2Error):
            continue
        byte_offsets = [0]
        for character in text:
            byte_offsets.append(byte_offsets[-1] + len(character.encode("utf-8")))
        for start, end, passage in _text_chunks(text):
            byte_start = byte_offsets[start]
            byte_end = byte_offsets[end]
            evidence = {
                "source_id": descriptor["id"],
                "source_version": descriptor["version"],
                "text_version": descriptor["text_version"],
                "byte_start": byte_start,
                "byte_end": byte_end,
                "excerpt_hash": hash_bytes(text_bytes[byte_start:byte_end]),
                "source_family_id": descriptor["source_family_id"],
            }
            passages.append(
                {
                    "identity": _source_identity(str(source_id), str(version), start, end),
                    "kind": "source",
                    "source_id": str(source_id),
                    "source_version": str(version),
                    "text_version": str(descriptor["text_version"]),
                    "text": passage,
                    "start": start,
                    "end": end,
                    "evidence": evidence,
                    "label": str(descriptor.get("origin") or source_id),
                    "source_family_id": str(descriptor.get("source_family_id") or source_id),
                }
            )
    return passages


class SemanticRuntime:
    """A caller-owned warm embedder with a bounded query-vector cache."""

    def __init__(self, vault: Path, embedder: Any, *, cache_size: int = 128) -> None:
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or not 1 <= cache_size <= _MAX_CACHE:
            raise V2Error("invalid-request", "Semantic runtime cache_size must be between 1 and 256")
        if _model(embedder) is None:
            raise V2Error("invalid-request", "A semantic runtime requires an embedder model name")
        self.vault = Path(vault).resolve()
        self.embedder = embedder
        self.model = _model(embedder)
        self.cache_size = cache_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._validated: OrderedDict[tuple, list] = OrderedDict()
        self._lock = threading.RLock()

    def query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text:
            raise V2Error("invalid-request", "Semantic query text must be nonempty")
        with self._lock:
            if text in self._cache:
                value = self._cache.pop(text)
                self._cache[text] = value
                return list(value)
        vector = _call_embedder(self.embedder, text)
        with self._lock:
            self._cache[text] = vector
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return list(vector)

    def close(self) -> None:
        self._cache.clear()
        self._validated.clear()
        if _RUNTIME_REGISTRY.get(self.vault) is self:
            _RUNTIME_REGISTRY.pop(self.vault, None)


def register_runtime(runtime: SemanticRuntime) -> SemanticRuntime:
    """Register an explicitly created runtime for module-level queries."""

    if not isinstance(runtime, SemanticRuntime):
        raise V2Error("invalid-request", "register_runtime requires SemanticRuntime")
    _RUNTIME_REGISTRY[runtime.vault] = runtime
    return runtime


def get_runtime(vault: Path) -> SemanticRuntime | None:
    """Return the explicitly registered runtime for one resolved vault."""

    return _RUNTIME_REGISTRY.get(Path(vault).resolve())


def _call_embedder(embedder: Any, text: str) -> list[float]:
    def invoke() -> Any:
        query_method = getattr(embedder, "query", None)
        if callable(query_method):
            raw = query_method(text)
            # Adapters sometimes return a one-item batch from query().
            if isinstance(raw, Sequence) and raw and isinstance(raw[0], Sequence):
                raw = raw[0]
            return raw
        method = getattr(embedder, "embed", None)
        if not callable(method):
            raise TypeError("embedder requires query(text) or embed([text])")
        batch = method([text])
        if not isinstance(batch, Sequence) or len(batch) != 1:
            raise ValueError("embedder returned the wrong number of query vectors")
        return batch[0]

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synapse-semantic-query")
    future = executor.submit(invoke)
    try:
        raw = future.result(timeout=_QUERY_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError("semantic query embedding timed out") from exc
    except V2Error:
        raise
    except Exception as exc:
        raise V2Error(
            "source-unavailable",
            "Semantic query embedding failed",
            details={"reason": type(exc).__name__},
        ) from exc
    finally:
        # A direct model may not be cancellable, but the caller must still
        # regain control at the deadline.  Explicit production workers such
        # as EmbeddingWorker enforce their own process boundary as well.
        executor.shutdown(wait=False, cancel_futures=True)
    vector = _vector(raw)
    if vector is None:
        raise V2Error("source-unavailable", "Semantic query returned an invalid vector")
    return vector


def _record_texts(
    store: RevisionStore,
    manifest: Mapping[str, Any],
) -> list[tuple[str, str, str, bytes, str, dict[str, Any]]]:
    records: list[tuple[str, str, str, bytes, str, dict[str, Any]]] = []
    for identity, descriptor in sorted(manifest["records"].items()):
        if not descriptor.get("active"):
            continue
        version = str(descriptor["version"])
        raw = store.read_object(version)
        text, text_hash = _embedding_row(str(identity), version, raw)
        records.append(
            (str(identity), version, text_hash, text.encode("utf-8"), "record", {"id": str(identity), "version": version})
        )
    return records


def _source_rows(view: ReadView) -> list[tuple[str, str, str, bytes, str, dict[str, Any]]]:
    rows: list[tuple[str, str, str, bytes, str, dict[str, Any]]] = []
    for passage in source_passages(view):
        text = str(passage["text"])
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        ref = {
            "source_id": passage["source_id"],
            "source_version": passage["source_version"],
            "text_version": passage["text_version"],
            "evidence": [passage["evidence"]],
        }
        rows.append(
            (passage["identity"], passage["source_version"], text_hash, text.encode("utf-8"), "source", ref)
        )
    return rows


def _report(
    revision: str,
    path: Path,
    *,
    status: str,
    model: str | None = None,
    dimension: int | None = None,
    embedded: int = 0,
    omissions: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "knowledge_revision": revision,
        "revision": revision,
        "path": str(path),
        "index_state": "current" if status == "ok" else "unavailable",
        "semantic": status,
        "semantic_search": status,
        "model": model,
        "dimensions": dimension,
        "embedded": embedded,
        "omissions": list(omissions),
    }


def build_index(
    vault: Path,
    *,
    revision: str | None = None,
    embedder: Any = None,
) -> dict[str, Any]:
    """Build one atomic revision-specific semantic index.

    No embedder is loaded here.  ``embedder=None`` may use an explicitly
    registered runtime, otherwise preparation is refused.  Hash fallback
    vectors are intentionally refused as semantic retrieval.
    """

    vault = Path(vault).resolve()
    store = RevisionStore(vault)
    pinned = revision or store.head()
    manifest = store.manifest(pinned)
    runtime = get_runtime(vault) if embedder is None else None
    if embedder is None and runtime is not None:
        embedder = runtime.embedder
    if isinstance(embedder, SemanticRuntime):
        if embedder.vault != vault:
            return _report(pinned, _index_path(vault, pinned), status="refused:runtime-vault-mismatch", omissions=["Runtime is registered for a different vault."])
        embedder = embedder.embedder
    path = _index_path(vault, pinned)
    model = _model(embedder)
    if embedder is None:
        return _report(pinned, path, status="refused:no-warm-runtime", omissions=["No explicitly prepared embedder was supplied."])
    if model is None:
        return _report(pinned, path, status="refused:invalid-embedder", omissions=["Embedder has no model name."])
    if model == "hash-local-test":
        return _report(pinned, path, model=model, status="refused:hash-fallback", omissions=["Hash-local vectors are not semantic evidence."])

    records = _record_texts(store, manifest)
    records.extend(_source_rows(ReadView(vault, revision=pinned)))
    texts = [item[3].decode("utf-8") for item in records]
    try:
        method = getattr(embedder, "embed", None)
        if not callable(method):
            return _report(pinned, path, model=model, status="refused:invalid-embedder", omissions=["Index preparation requires embed(texts)."])
        raw_vectors = method(texts) if texts else []
        if not isinstance(raw_vectors, Sequence) or len(raw_vectors) != len(records):
            return _report(pinned, path, model=model, status="refused:invalid-vector", omissions=["Embedder returned an incomplete vector batch."])
        vectors = [_vector(value) for value in raw_vectors]
        if any(value is None for value in vectors):
            return _report(pinned, path, model=model, status="refused:invalid-vector", omissions=["Embedder returned an empty, nonnumeric or nonfinite vector."])
        checked_vectors = [value for value in vectors if value is not None]
        dimension = len(checked_vectors[0]) if checked_vectors else 0
        if dimension == 0 or any(len(value) != dimension for value in checked_vectors):
            return _report(pinned, path, model=model, dimension=dimension, status="refused:dimension-mismatch", omissions=["Vectors do not share one positive dimension."])
    except Exception as exc:
        return _report(pinned, path, model=model, status="refused:embed-error", omissions=[type(exc).__name__])

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{pinned}.", suffix=".sqlite", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode = DELETE;
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE vectors (
                    identity TEXT PRIMARY KEY,
                    object_version TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    kind TEXT NOT NULL,
                    ref_json TEXT NOT NULL
                );
                CREATE INDEX vectors_version ON vectors(object_version);
                """
            )
            meta = {
                "schema": _SCHEMA,
                "knowledge_revision": pinned,
                "model": model,
                "dimensions": str(dimension),
                "candidate_kinds": "record,source",
            }
            connection.executemany("INSERT INTO meta(key,value) VALUES (?,?)", meta.items())
            connection.executemany(
                "INSERT INTO vectors(identity,object_version,text_hash,model,dimensions,vector,kind,ref_json) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (identity, version, text_hash, model, dimension, vector_to_blob(vector), kind, json.dumps(ref, sort_keys=True))
                    for (identity, version, text_hash, _text, kind, ref), vector in zip(records, checked_vectors, strict=True)
                ],
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        return _report(pinned, path, model=model, dimension=dimension, status="refused:index-write", omissions=["Semantic index could not be published atomically."])
    return _report(pinned, path, model=model, dimension=dimension, embedded=len(records), status="ok")


def _read_meta(connection: sqlite3.Connection) -> dict[str, str] | None:
    try:
        rows = connection.execute("SELECT key,value FROM meta").fetchall()
    except sqlite3.Error:
        return None
    result = {str(key): str(value) for key, value in rows}
    return result if len(result) == len(rows) else None


def _validated_vectors(
    view: ReadView,
    connection: sqlite3.Connection,
    *,
    model: str,
    include_sources: bool,
) -> tuple[list[tuple[str, str, list[float], str, dict[str, Any]]], list[str]]:
    meta = _read_meta(connection)
    if meta is None:
        return [], ["semantic-index-metadata-corrupt"]
    if meta.get("schema") != _SCHEMA:
        return [], ["semantic-index-schema-mismatch"]
    if meta.get("knowledge_revision") != view.revision:
        return [], ["semantic-index-revision-mismatch"]
    if meta.get("model") != model:
        return [], ["semantic-index-model-mismatch"]
    try:
        dimensions = int(meta["dimensions"])
    except (KeyError, ValueError):
        return [], ["semantic-index-dimension-corrupt"]
    if dimensions <= 0:
        return [], ["semantic-index-dimension-corrupt"]
    try:
        query = "SELECT identity,object_version,text_hash,model,dimensions,vector,kind,ref_json FROM vectors"
        parameters: tuple[str, ...] = ()
        if not include_sources:
            query += " WHERE kind = ?"
            parameters = ("record",)
        rows = connection.execute(query + " ORDER BY identity", parameters).fetchall()
    except sqlite3.Error:
        return [], ["semantic-index-vectors-corrupt"]

    passages = source_passages(view) if include_sources else []
    passages_by_id = {item["identity"]: item for item in passages}
    expected = {
        identity for identity, row in view.manifest["records"].items() if row.get("active")
    }
    expected.update(passages_by_id)
    actual = {str(row[0]) for row in rows}
    if actual != expected:
        return [], ["semantic-index-record-set-mismatch"]
    vectors: list[tuple[str, str, list[float], str, dict[str, Any]]] = []
    for row in rows:
        identity, version, text_hash, row_model, row_dimensions, blob, kind, ref_json = row
        descriptor = view.manifest["records"].get(identity)
        if descriptor is not None:
            expected_version = descriptor["version"]
            expected_kind = "record"
        else:
            passage = passages_by_id.get(identity)
            expected_version = passage["source_version"] if passage else None
            expected_kind = "source"
        if (
            expected_version != version
            or kind != expected_kind
            or row_model != model
            or int(row_dimensions) != dimensions
        ):
            return [], [f"semantic-index-stale:{identity}"]
        if not isinstance(blob, (bytes, bytearray)) or len(blob) != dimensions * 4:
            return [], [f"semantic-index-vector-length:{identity}"]
        try:
            vector = blob_to_vector(bytes(blob))
        except (TypeError, ValueError, OverflowError):
            return [], [f"semantic-index-vector-corrupt:{identity}"]
        if len(vector) != dimensions or any(not math.isfinite(float(value)) for value in vector):
            return [], [f"semantic-index-vector-invalid:{identity}"]
        if kind == "record":
            raw = view.store.read_object(version)
            _text, actual_text_hash = _embedding_row(identity, version, raw)
        else:
            passage = passages_by_id.get(identity)
            if passage is None:
                return [], [f"semantic-index-source-unavailable:{identity}"]
            actual_text_hash = hashlib.sha256(str(passage["text"]).encode("utf-8")).hexdigest()
        if actual_text_hash != text_hash:
            return [], [f"semantic-index-text-mismatch:{identity}"]
        try:
            reference = json.loads(ref_json)
        except (TypeError, json.JSONDecodeError):
            return [], [f"semantic-index-reference-corrupt:{identity}"]
        if not isinstance(reference, dict):
            return [], [f"semantic-index-reference-corrupt:{identity}"]
        if kind == "source":
            passage = passages_by_id.get(identity)
            expected_reference = {
                "source_id": passage["source_id"],
                "source_version": passage["source_version"],
                "text_version": passage["text_version"],
                "evidence": [passage["evidence"]],
            } if passage is not None else None
            if reference != expected_reference:
                return [], [f"semantic-index-reference-mismatch:{identity}"]
        elif reference != {"id": str(identity), "version": str(version)}:
            return [], [f"semantic-index-reference-mismatch:{identity}"]
        vectors.append((str(identity), str(version), [float(value) for value in vector], str(kind), reference))
    return vectors, []


def prepared_vectors(view: ReadView, *, model: str, runtime: SemanticRuntime | None = None, include_sources: bool = True) -> dict[str, Any]:
    """Validate existing local vectors without running any embedding model.

    Internal organization uses this read seam, then nominates bounded candidate
    pairs. It must not issue an all-vault semantic query for every member.
    """
    if model == "hash-local-test":
        return _refusal(view.revision, "hash-fallback")
    if runtime is not None and runtime.vault != view.vault:
        return _refusal(view.revision, "runtime-vault-mismatch")
    path = _index_path(view.vault, view.revision)
    if not path.is_file():
        return _refusal(view.revision, "index-missing", omissions=["Run explicit semantic index preparation for this revision."])
    try:
        # Validate exact bindings once per unchanged immutable-object/index
        # snapshot. Any object replacement or index edit invalidates reuse.
        def stamp(file):
            stat = file.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        signature = (
            view.revision,
            model,
            include_sources,
            stamp(path),
            tuple(
                (row["version"], stamp(view.store.root / "objects" / row["version"]))
                for row in view.manifest["records"].values()
                if row["active"]
            ),
            tuple(
                (
                    version,
                    descriptor.get("original_hash"),
                    descriptor.get("text_version"),
                    stamp(view.store.root / "objects" / descriptor["original_hash"]),
                    stamp(view.store.root / "objects" / descriptor["text_version"]),
                )
                for version, descriptor in sorted(view.manifest.get("source_versions", {}).items())
                if include_sources and version in set(view.manifest.get("sources", {}).values()) and descriptor.get("text_version")
            ),
        )
        vectors = runtime._validated.get(signature) if runtime else None
        omissions = []
        if vectors is None:
            connection = sqlite3.connect(path)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    return _refusal(view.revision, "index-corrupt")
                vectors, omissions = _validated_vectors(
                    view,
                    connection,
                    model=model,
                    include_sources=include_sources,
                )
            finally:
                connection.close()
            if runtime and not omissions:
                with runtime._lock:
                    runtime._validated[signature] = vectors
                    while len(runtime._validated) > 2:
                        runtime._validated.popitem(last=False)
    except (OSError, sqlite3.Error, V2Error, ValueError, TypeError) as exc:
        return _refusal(view.revision, "index-corrupt", omissions=[type(exc).__name__])
    if omissions:
        reason = "stale-index"
        if any("model-mismatch" in item for item in omissions):
            reason = "semantic-index-model-mismatch"
        elif any("revision-mismatch" in item for item in omissions):
            reason = "semantic-index-revision-mismatch"
        elif any("schema-mismatch" in item for item in omissions):
            reason = "semantic-index-schema-mismatch"
        return _refusal(view.revision, reason, omissions=omissions)

    return {"semantic": "ok", "vectors": vectors, "omissions": omissions, "model": model}


def query(
    view: ReadView,
    text: str,
    *,
    subject_id: str | None = None,
    facet: str | None = None,
    availability: str | None = None,
    limit: int = 20,
    embedder: Any = None,
    include_sources: bool = False,
    source_scope: str = "ordinary",
) -> dict[str, Any]:
    """Query a prepared semantic index without loading or refreshing models.

    Record candidates remain the default for compatibility with existing
    Gateway callers.  Callers that can consume typed source members may opt in
    with ``include_sources=True``.
    """

    if not isinstance(view, ReadView):
        raise V2Error("invalid-request", "query requires a pinned ReadView")
    if not isinstance(text, str) or not text.strip():
        raise V2Error("invalid-request", "Semantic query text must be nonempty")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise V2Error("invalid-request", "Semantic query limit must be between 1 and 200")
    if not isinstance(include_sources, bool):
        raise V2Error("invalid-request", "include_sources must be boolean")
    validate_source_scope(source_scope)
    # Policy is applied per query, independently of immutable vector caching.
    # Retain all passages in prepared indexes for exact/diagnostic inspection.
    policy = view.source_purpose_policy() if include_sources else None
    for name, value in (("subject_id", subject_id), ("facet", facet), ("availability", availability)):
        if value is not None and not isinstance(value, str):
            raise V2Error("invalid-request", f"{name} must be a string")

    runtime = embedder if isinstance(embedder, SemanticRuntime) else None
    if embedder is None:
        runtime = get_runtime(view.vault)
        embedder = runtime
    if embedder is None:
        return _refusal(view.revision, "no-warm-runtime", omissions=["No explicitly prepared embedder or registered runtime is available."])
    if runtime is not None and runtime.vault != view.vault:
        return _refusal(view.revision, "runtime-vault-mismatch")
    model = _model(embedder)
    if model is None:
        return _refusal(view.revision, "invalid-embedder")
    if model == "hash-local-test":
        return _refusal(view.revision, "hash-fallback", omissions=["Hash-local vectors are not semantic evidence."])

    loaded = prepared_vectors(view, model=model, runtime=runtime, include_sources=include_sources)
    if loaded["semantic"] != "ok":
        return loaded
    vectors = loaded["vectors"]

    try:
        query_vector = runtime.query(text) if runtime is not None else _call_embedder(embedder, text)
    except TimeoutError:
        return _refusal(view.revision, "timeout", omissions=["Query embedding exceeded the bounded semantic deadline."])
    except V2Error as exc:
        reason = (exc.details or {}).get("reason", "embedding-error")
        return _refusal(view.revision, "timeout" if reason == "TimeoutError" else "embedding-error", omissions=[str(reason)])
    dimension = len(query_vector)
    if not dimension or any(not math.isfinite(value) for value in query_vector):
        return _refusal(view.revision, "invalid-query-vector")
    if any(len(vector) != dimension for _identity, _version, vector, _kind, _ref in vectors):
        return _refusal(view.revision, "dimension-mismatch")

    scoped: list[tuple[str, str, list[float], str, dict[str, Any]]] = []
    limitations: list[str] = []
    try:
        connection = view._connect()
        try:
            where, parameters, _ = view._record_where(subject_id=subject_id, facet=facet, availability=availability, query=None)
            eligible = {row[0] for row in connection.execute(f"SELECT r.id FROM records r WHERE {where}", parameters)}
            # A suggested 'revises' reference qualifies the old assertion; it
            # cannot supersede an accepted record before owner adoption.
            anchored_sources: set[tuple[str, str]] | None = None
            if include_sources and any(value is not None for value in (subject_id, facet, availability)):
                anchored_sources = set()
                if eligible:
                    placeholders = ",".join("?" for _ in eligible)
                    rows = connection.execute(
                        f"SELECT record_json FROM records WHERE id IN ({placeholders})",
                        tuple(sorted(eligible)),
                    ).fetchall()
                    for row in rows:
                        payload = json.loads(row[0])
                        for reference in payload.get("evidence", []) if isinstance(payload, dict) else []:
                            if isinstance(reference, dict) and isinstance(reference.get("source_id"), str):
                                source_version = reference.get("source_version")
                                if isinstance(source_version, str):
                                    anchored_sources.add((reference["source_id"], source_version))
                limitations.append("Scoped source candidates include only retained sources anchored by eligible scoped records; source-only material is excluded from this scoped semantic result.")
        finally:
            connection.close()
        scoped = [
            item
            for item in vectors
            if (item[3] == "record" and item[0] in eligible)
            or (
                item[3] == "source"
                and policy is not None
                and policy.visible(item[4].get("source_id"), item[4].get("source_version"), source_scope)
                and (
                    anchored_sources is None
                    or (item[4].get("source_id"), item[4].get("source_version")) in anchored_sources
                )
            )
        ]
    except (OSError, V2Error, TypeError, ValueError) as exc:
        return _refusal(view.revision, "record-corrupt", omissions=[type(exc).__name__])

    ranked = sorted(
        (
            (cosine(query_vector, vector), identity, version, kind, reference)
            for identity, version, vector, kind, reference in scoped
        ),
        key=lambda item: (-item[0], item[1]),
    )[:limit]
    candidates = []
    for score, identity, version, kind, reference in ranked:
        candidate = {
            "identity": identity,
            "id": identity,
            "version": version,
            "kind": kind,
            "score": float(score),
            **reference,
        }
        if kind == "source":
            candidate["chunk_id"] = identity
            candidate.update(policy.labels(reference["source_id"], reference["source_version"]))
        candidates.append(candidate)
    return {
        "candidates": candidates,
        "semantic": "ok",
        "semantic_search": "ok",
        "knowledge_revision": view.revision,
        "revision": view.revision,
        "index_state": "current",
        "omissions": [],
        "fallback": None,
        "limitations": limitations,
        "source_scope": source_scope,
        **({"source_policy": policy.metadata()} if policy is not None else {}),
    }
