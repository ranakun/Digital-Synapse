"""Explicit lifecycle for reused, bounded local semantic inference."""

from contextlib import contextmanager

from synapse.config import load_config
from synapse.retrieval_runtime import EmbeddingWorker
from synapse.v2_contracts import V2Error
from synapse.v2_semantic import SemanticRuntime, register_runtime


@contextmanager
def warm_semantics(vault, *, enabled=True):
    worker = runtime = None
    try:
        if enabled:
            config = load_config(vault)["embeddings"]
            if config.get("provider") != "fastembed":
                raise V2Error("unsupported-operation", "Semantic inference requires the configured local FastEmbed model")
            worker = EmbeddingWorker(config["model"])
            worker.start()  # only this explicit lifecycle may load the model
            runtime = register_runtime(SemanticRuntime(vault, worker))
        yield runtime
    finally:
        if runtime:
            runtime.close()
        if worker:
            worker.close()
