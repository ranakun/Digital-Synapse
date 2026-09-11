"""Pytest configuration: add the repo-level scripts/ directory to sys.path
so that tests can import helpers like ``gen_synthetic_vault`` directly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Make scripts/ importable as a plain module (not a package).
# This is safe: the scripts directory contains only standalone helpers with no
# name collisions with installed packages.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _is_codex_command(command: object) -> bool:
    if isinstance(command, (list, tuple)) and command:
        executable = command[0]
    elif isinstance(command, str):
        executable = command.split(maxsplit=1)[0] if command else ""
    else:
        return False
    return Path(str(executable)).name.casefold() in {"codex", "codex.exe"}


@pytest.fixture(autouse=True)
def _block_external_model_and_codex_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests deterministic while preserving local subprocess and MCP coverage."""
    from synapse.embeddings import FastEmbedder

    original_popen = subprocess.Popen
    original_run = subprocess.run

    def block_fastembedder(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Tests must inject an embedder; FastEmbedder may download a model")

    class GuardedPopen(original_popen):
        def __init__(self, *args: object, **kwargs: object) -> None:
            command = args[0] if args else kwargs.get("args")
            if _is_codex_command(command):
                pytest.fail("Tests must inject a synthetic Codex transport")
            super().__init__(*args, **kwargs)

    def guarded_run(*args: object, **kwargs: object):
        if args and _is_codex_command(args[0]):
            pytest.fail("Tests must inject a synthetic Codex transport")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(FastEmbedder, "__init__", block_fastembedder)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(subprocess, "run", guarded_run)
