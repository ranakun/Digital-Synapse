"""Readiness discovery without model inference or index preparation."""

from synapse.v2_semantic import get_runtime, prepared_vectors


def semantic_capability(view):
    runtime = get_runtime(view.vault)
    if runtime is None:
        return {"state": "unavailable", "reason": "runtime-not-warmed", "knowledge_revision": view.revision}
    prepared = prepared_vectors(view, model=runtime.model, runtime=runtime)
    reason = prepared["semantic"]
    state = "ready" if reason == "ok" else "stale" if any(word in reason for word in ("stale", "mismatch")) else "unavailable"
    return {"state": state, "reason": reason, "model": runtime.model, "knowledge_revision": view.revision}
