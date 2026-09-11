from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from synapse.knowledge import record_descriptor
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.v2_cli import app
from synapse.v2_contracts import hash_bytes


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    raw = (
        b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOwner profile.\n"
    )
    row = record_descriptor(raw, path="entities/people/me.md")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"seed"),
        mutate=lambda manifest, _read: manifest["records"].update({"me": row}),
        objects={row["version"]: raw},
        initialize=True,
    )
    return vault


def test_read_cli_is_json_and_unknown_operation_does_not_write(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["read", "describe", "--vault", str(vault), "--budget-chars", "8000"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["methods"]["search_sources"]

    failed = runner.invoke(app, ["read", "publish", "--vault", str(vault)])
    assert failed.exit_code == 1
    assert json.loads(failed.output)["error"]["code"] == "unsupported-operation"
    assert not (vault / "_synapse" / "runs").exists()


def test_direct_ask_uses_an_explicitly_injected_reasoner(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path)

    class FakeReasoner:
        def __init__(self, *, model=None):
            self.model = model

        def step(self, _context, *, timeout, cancelled):
            assert timeout > 0
            assert not cancelled()
            return {
                "action": "finish",
                "calls": [],
                "answer": "Synthetic answer.",
                "used_record_ids": [],
                "alternatives": [],
                "uncertainties": ["Synthetic coverage only."],
                "findings_json": "[]",
                "stop_reason": "Synthetic test completion.",
            }

    monkeypatch.setattr("synapse.v2_cli.CodexReasoner", FakeReasoner)
    result = CliRunner().invoke(
        app,
        [
            "ask",
            "What is known?",
            "--vault",
            str(vault),
            "--model",
            "explicit-test-model",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["answer"] == "Synthetic answer."
    assert payload["uncertainties"] == ["Synthetic coverage only."]
