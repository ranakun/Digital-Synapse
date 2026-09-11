"""Explicit generator contract for tests and caller-supplied offline transforms.

Digital Synapse does not construct a network provider. Production judgement is
performed by the owner's subscription-authenticated Codex task, which writes
reviewable proposals or calls the deterministic read surfaces directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class CompletionRequest:
    task: str
    prompt: str
    schema_name: str


@dataclass
class CompletionResult:
    data: dict[str, Any]
    model: str
    raw: str


class Generator(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResult: ...


class AgentRequiredError(RuntimeError):
    """Raised when a legacy free-form path needs Codex judgement."""


def require_generator(generator: Generator | None, *, task: str) -> Generator:
    if generator is None:
        raise AgentRequiredError(
            f"Free-form {task} is delegated to Codex and has no API-key fallback. "
            "Use a deterministic importer/read command or have Codex create a reviewable proposal."
        )
    return generator


class StaticGenerator:
    """Deterministic adapter for unit tests and fully local scripted transforms."""

    def __init__(self, data: dict[str, Any], model: str = "static-fixture") -> None:
        self.data = data
        self.model = model

    def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(data=self.data, model=self.model, raw=json.dumps(self.data))
