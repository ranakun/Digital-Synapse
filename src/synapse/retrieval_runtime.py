"""Long-lived retrieval runtime for shared MCP servers.

The HTTP MCP service is intentionally process-persistent.  It keeps one bounded
embedding worker and one exact, in-memory view of the disposable embedding
table so multiple local agents do not each load their own ONNX runtime or
deserialize every stored vector on every query.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from synapse.config import load_config
from synapse.embeddings import FastEmbedder, HashEmbedder
from synapse.index import connect

# The matrices are tiny (~3,200 x 384). Large BLAS thread pools add memory and
# scheduler contention without useful speedup, especially when several agents
# search at once. ONNX query inference has its own explicit two-thread limit.
for _thread_env in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_env] = "1"

WorkerTarget = Callable[[Any, Any, str, int], None]


def _embedding_worker_main(
    requests: Any,
    responses: Any,
    model: str,
    threads: int,
) -> None:
    """Own the only FastEmbed/ONNX session used by the shared MCP service."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        embedder = FastEmbedder(
            model,
            threads=threads,
            local_files_only=True,
        )
        # Force lazy tokenizer/runtime work to happen before the service is
        # advertised as warm.
        embedder.embed(["synapse readiness probe"])
        responses.put(("ready", None))
    except BaseException as exc:  # pragma: no cover - exercised through parent
        responses.put(("startup-error", f"{type(exc).__name__}: {exc}"))
        return

    while True:
        request = requests.get()
        if request is None:
            return
        request_id, texts = request
        try:
            responses.put(("result", request_id, embedder.embed(texts)))
        except BaseException as exc:  # pragma: no cover - exercised through parent
            responses.put(
                ("error", request_id, f"{type(exc).__name__}: {exc}")
            )


class EmbeddingWorker:
    """Serialize tiny query embeddings through one killable worker process."""

    def __init__(
        self,
        model: str,
        *,
        threads: int = 2,
        request_timeout: float = 1.5,
        startup_timeout: float = 10.0,
        worker_target: WorkerTarget = _embedding_worker_main,
    ) -> None:
        self.model = model
        self.threads = threads
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self._worker_target = worker_target
        self._ctx = mp.get_context("spawn")
        self._call_lock = threading.Lock()
        self._process: Any = None
        self._requests: Any = None
        self._responses: Any = None
        self._ready = False
        self._request_id = 0
        self._closed = False

    def _spawn_locked(self) -> None:
        if self._closed:
            raise RuntimeError("embedding worker is closed")
        if self._process is not None and self._process.is_alive():
            return
        self._requests = self._ctx.Queue(maxsize=1)
        self._responses = self._ctx.Queue(maxsize=2)
        self._process = self._ctx.Process(
            target=self._worker_target,
            args=(self._requests, self._responses, self.model, self.threads),
            name="synapse-embedding-worker",
            daemon=True,
        )
        self._process.start()
        self._ready = False

    def _await_ready_locked(self, timeout: float) -> None:
        if self._ready:
            return
        self._spawn_locked()
        try:
            message = self._responses.get(timeout=max(0.001, timeout))
        except queue.Empty as exc:
            self._restart_locked()
            raise TimeoutError("embedding worker startup timed out") from exc
        if message[0] != "ready":
            detail = message[1] if len(message) > 1 else "unknown startup error"
            self._terminate_locked()
            raise RuntimeError(f"embedding worker failed to start: {detail}")
        self._ready = True

    def _terminate_locked(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=0.1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=0.1)
        for channel in (self._requests, self._responses):
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
        self._process = None
        self._requests = None
        self._responses = None
        self._ready = False

    def _restart_locked(self) -> None:
        self._terminate_locked()
        # Spawning is cheap and non-blocking.  Do not wait here: the timed-out
        # caller must return promptly, while the replacement warms in parallel.
        self._spawn_locked()

    def start(self) -> None:
        """Eagerly start and warm the worker."""
        acquired = self._call_lock.acquire(timeout=self.startup_timeout)
        if not acquired:
            raise TimeoutError("embedding worker is busy during startup")
        try:
            self._await_ready_locked(self.startup_timeout)
        finally:
            self._call_lock.release()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Only the explicit service prewarm gets the longer startup deadline.
        # Live calls always include queue wait, recovery, and inference in one
        # short absolute deadline.
        wait_timeout = self.request_timeout
        started = time.monotonic()
        acquired = self._call_lock.acquire(timeout=wait_timeout)
        if not acquired:
            raise TimeoutError("embedding worker queue timed out")
        try:
            deadline = started + wait_timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("embedding worker queue timed out")
            self._await_ready_locked(remaining)

            self._request_id += 1
            request_id = self._request_id
            try:
                self._requests.put(
                    (request_id, texts),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except queue.Full as exc:
                self._restart_locked()
                raise TimeoutError("embedding worker queue timed out") from exc
            try:
                message = self._responses.get(
                    timeout=max(0.001, deadline - time.monotonic())
                )
            except queue.Empty as exc:
                self._restart_locked()
                raise TimeoutError("embedding inference timed out") from exc

            kind, response_id, payload = message
            if response_id != request_id:
                self._restart_locked()
                raise RuntimeError("embedding worker returned an out-of-order response")
            if kind == "error":
                raise RuntimeError(f"embedding worker failed: {payload}")
            if kind != "result":
                raise RuntimeError(f"unexpected embedding worker response: {kind}")
            return payload
        finally:
            self._call_lock.release()

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    def close(self) -> None:
        with self._call_lock:
            if self._closed:
                return
            self._closed = True
            if self._process is not None and self._process.is_alive() and self._ready:
                try:
                    self._requests.put_nowait(None)
                    self._process.join(timeout=0.5)
                except Exception:
                    pass
            self._terminate_locked()


class ExactVectorIndex:
    """An exact NumPy cosine index refreshed via SQLite's data-version signal."""

    def __init__(self, vault: str | Path) -> None:
        self.vault = Path(vault).resolve()
        self._lock = threading.Lock()
        self._conn: Any = None
        self._data_version: int | None = None
        self._matrix: Any = None
        self._rows: list[dict[str, Any]] = []
        self._models: set[str] = set()

    def _connection_locked(self) -> Any:
        if self._conn is None:
            self._conn = connect(self.vault, check_same_thread=False)
        return self._conn

    def _reload_locked(self) -> None:
        import numpy as np

        conn = self._connection_locked()
        try:
            conn.execute("BEGIN")
            data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            rows = conn.execute(
                """
                SELECT e.id, e.name, e.type, e.body,
                       emb.model, emb.dim, emb.vector
                FROM embeddings emb
                JOIN entities e ON e.id = emb.entity_id
                ORDER BY e.id
                """
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        snapshot_rows = [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "body": row["body"],
            }
            for row in rows
        ]
        models = {row["model"] for row in rows if row["model"]}
        if rows:
            dimensions = {int(row["dim"]) for row in rows}
            if len(models) != 1 or len(dimensions) != 1:
                raise ValueError("embedding snapshot has mixed models or dimensions")
            dimension = next(iter(dimensions))
            vectors = []
            for row in rows:
                vector = np.frombuffer(row["vector"], dtype=np.float32)
                if vector.shape != (dimension,):
                    raise ValueError(
                        f"embedding {row['id']} has an invalid vector length"
                    )
                vectors.append(vector)
            matrix = np.vstack(vectors)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            snapshot_matrix = np.divide(
                matrix,
                norms,
                out=np.zeros_like(matrix),
                where=norms != 0,
            )
        else:
            snapshot_matrix = None

        # Publish the completed immutable snapshot as one critical-section
        # update; concurrent callers can never observe a half-built cache.
        self._rows = snapshot_rows
        self._models = models
        self._matrix = snapshot_matrix
        self._data_version = data_version

    def preload(self) -> None:
        with self._lock:
            self._reload_locked()

    def invalidate(self) -> None:
        with self._lock:
            self._data_version = None

    def search(
        self,
        query_vector: list[float],
        *,
        model: str,
        entity_type: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        import numpy as np

        with self._lock:
            conn = self._connection_locked()
            current_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if self._matrix is None or self._data_version != current_version:
                self._reload_locked()
            if self._matrix is None:
                return []
            if self._models != {model}:
                raise ValueError("embedding snapshot model does not match query model")

            query = np.asarray(query_vector, dtype=np.float32)
            if query.ndim != 1 or query.shape[0] != self._matrix.shape[1]:
                raise ValueError("query embedding dimension does not match the index")
            norm = float(np.linalg.norm(query))
            if not norm:
                return []
            scores = self._matrix @ (query / norm)
            if entity_type:
                candidates = np.asarray(
                    [
                        index
                        for index, row in enumerate(self._rows)
                        if row["type"] == entity_type
                    ],
                    dtype=np.int64,
                )
            else:
                candidates = np.arange(len(self._rows), dtype=np.int64)
            if not len(candidates):
                return []

            count = min(limit, len(candidates))
            candidate_scores = scores[candidates]
            if count == len(candidates):
                top_local = np.argsort(candidate_scores)[::-1]
            else:
                top_local = np.argpartition(candidate_scores, -count)[-count:]
                top_local = top_local[np.argsort(candidate_scores[top_local])[::-1]]
            return [
                {
                    "row": self._rows[int(candidates[int(index)])],
                    "score": float(candidate_scores[int(index)]),
                }
                for index in top_local[:count]
            ]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._matrix = None
            self._rows = []
            self._models = set()
            self._data_version = None


class SearchRuntime:
    """Own the shared MCP query embedder and exact semantic index."""

    def __init__(self, vault: str | Path) -> None:
        self.vault = Path(vault).resolve()
        cfg = load_config(self.vault)
        embeddings = cfg.get("embeddings") or {}
        if embeddings.get("enabled") and embeddings.get("provider") == "fastembed":
            self.embedder: Any = EmbeddingWorker(str(embeddings["model"]))
        else:
            self.embedder = HashEmbedder()
        self.semantic_index = ExactVectorIndex(self.vault)

    def prewarm(self) -> None:
        if isinstance(self.embedder, EmbeddingWorker):
            self.embedder.start()
            self.semantic_index.preload()

    def invalidate(self) -> None:
        self.semantic_index.invalidate()

    def close(self) -> None:
        if isinstance(self.embedder, EmbeddingWorker):
            self.embedder.close()
        self.semantic_index.close()
