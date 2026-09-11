from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from synapse.cli import app
from synapse.config import init_vault
from synapse.generation import (
    AgentRequiredError,
    CompletionRequest,
    StaticGenerator,
    require_generator,
)


def test_runtime_has_no_implicit_api_generator() -> None:
    with pytest.raises(AgentRequiredError, match="delegated to Codex.*no API-key fallback"):
        require_generator(None, task="ingestion")


def test_explicit_static_generator_remains_available_for_local_tests() -> None:
    generator = StaticGenerator({"changeset": [], "ambiguities": []})
    request = CompletionRequest(task="ingest", schema_name="changeset", prompt="fixture")

    assert require_generator(generator, task="ingestion") is generator
    result = generator.complete(request)
    assert result.data == {"changeset": [], "ambiguities": []}
    assert json.loads(result.raw) == result.data


def test_cli_free_form_paths_direct_the_owner_to_codex(tmp_path) -> None:
    vault = init_vault(tmp_path / "vault", initialize_git=False)
    source = vault / "inbox" / "unstructured.txt"
    source.write_text("synthetic source", encoding="utf-8")
    runner = CliRunner()

    ingest = runner.invoke(app, ["ingest", str(source), "--vault", str(vault)])
    assert ingest.exit_code == 2
    assert "Ask Codex" in ingest.output

    watch = runner.invoke(app, ["watch", "--vault", str(vault)])
    assert watch.exit_code == 2
    assert "Use Codex" in watch.output

    query = runner.invoke(app, ["query", "Summarize my career", "--vault", str(vault)])
    assert query.exit_code == 1
    assert "delegated to Codex" in query.output
