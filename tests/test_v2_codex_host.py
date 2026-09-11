from __future__ import annotations

import json
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from test_v2_publication import _bootstrap

from synapse.codex_host import CodexReasoner
from synapse.gateway import Gateway
from synapse.host_session import NativeHost
from synapse.v2_contracts import V2Error

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _stub(tmp_path: Path, *, mode: str = "valid") -> tuple[Path, Path, Path]:
    log_path = tmp_path / "stub-argv.json"
    child_path = tmp_path / "stub-child.pid"
    script = tmp_path / "codex-stub.py"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3

            import json
            import os
            import subprocess
            import sys
            import time

            args = sys.argv[1:]
            log = os.environ.get("SYNAPSE_TEST_STUB_LOG")
            if log:
                with open(log, "w", encoding="utf-8") as stream:
                    json.dump({
                        "args": args,
                        "has_openai_key": "OPENAI_API_KEY" in os.environ,
                        "has_codex_key": "CODEX_API_KEY" in os.environ,
                    }, stream)
            mode = os.environ.get("SYNAPSE_TEST_STUB_MODE", "valid")
            if mode.startswith("schema-"):
                error = {"code": "invalid_json_schema", "message": "synthetic-secret schema diagnostic"}
                if mode == "schema-stderr":
                    print(json.dumps({"error": error}), file=sys.stderr)
                elif mode == "schema-message":
                    print(json.dumps({"type": "error", "message": json.dumps({"error": error})}))
                else:
                    print(json.dumps({"type": "turn.failed", "error": error}))
                raise SystemExit(0 if mode == "schema-zero" else 1)
            if mode == "exit":
                print("synthetic-secret unknown failure", file=sys.stderr)
                print(json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": "invalid_json_schema synthetic-secret"
                }}))
                raise SystemExit(7)
            if mode == "hang":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                child_path = os.environ["SYNAPSE_TEST_STUB_CHILD"]
                with open(child_path, "w", encoding="ascii") as stream:
                    stream.write(str(child.pid))
                while True:
                    time.sleep(1)
            output_path = args[args.index("--output-last-message") + 1]
            value = {} if mode == "invalid" else {"answer": "synthetic stub response"}
            if mode == "prepare":
                schema_path = args[args.index("--output-schema") + 1]
                with open(schema_path, encoding="utf-8") as stream:
                    schema = json.load(stream)
                evidence_schema = schema["properties"]["items"]["items"]["properties"]["evidence_indices"]
                if "uniqueItems" in evidence_schema:
                    print(json.dumps({"type": "error", "message": "invalid_json_schema: uniqueItems is not permitted"}))
                    raise SystemExit(1)
                value = {"items": [{
                    "record_kind": "question", "statement": "What might resolve this?",
                    "support": ["A retained note."], "would_change_with": [], "limits": [],
                    "evidence_indices": json.loads(os.environ.get("SYNAPSE_TEST_INDICES", "[1, 0]")),
                }]}
            with open(output_path, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
            print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log_path, child_path


def _args(log_path: Path) -> list[str]:
    return json.loads(log_path.read_text(encoding="utf-8"))["args"]


def _assert_dead(pid: int) -> None:
    for _ in range(30):
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
        state = result.stdout.strip()
        if not state or state.startswith("Z"):
            return
        time.sleep(0.1)
    pytest.fail(f"stub process {pid} survived process-group cancellation: {state!r}")


def test_codex_transport_uses_ephemeral_read_only_schema_checked_process_without_api_keys(
    tmp_path: Path, monkeypatch
) -> None:
    script, log_path, _child_path = _stub(tmp_path)
    monkeypatch.setenv("SYNAPSE_TEST_STUB_LOG", str(log_path))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-secret")

    result = CodexReasoner(model="synthetic-model", effort="low", executable=str(script)).complete(
        "synthetic prompt", SCHEMA, timeout=3
    )

    assert result == {"answer": "synthetic stub response"}
    logged = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged["has_openai_key"] is False
    assert logged["has_codex_key"] is False
    args = logged["args"]
    assert args[:8] == [
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
    ]
    assert "never" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert args[-1] == "-"
    for feature in ("shell_tool", "apps", "browser_use", "computer_use", "view_image", "skill_search"):
        assert args[args.index("--disable", args.index(feature) - 1) + 1] == feature
    assert 'web_search="disabled"' in args
    assert 'model_reasoning_effort="low"' in args
    assert "--model" in args and args[args.index("--model") + 1] == "synthetic-model"


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [("invalid", "invalid-request"), ("exit", "unsupported-operation")],
)
def test_codex_transport_rejects_invalid_output_and_nonzero_startup(
    tmp_path: Path, monkeypatch, mode: str, expected_code: str
) -> None:
    script, log_path, _child_path = _stub(tmp_path, mode=mode)
    monkeypatch.setenv("SYNAPSE_TEST_STUB_LOG", str(log_path))
    monkeypatch.setenv("SYNAPSE_TEST_STUB_MODE", mode)

    with pytest.raises(V2Error) as caught:
        CodexReasoner(executable=str(script)).complete("prompt", SCHEMA, timeout=2)
    assert caught.value.code == expected_code
    if mode == "exit":
        assert caught.value.details["exit_status"] == 7
        assert "sign-in" in str(caught.value)
    assert "synthetic-secret" not in str(caught.value)
    assert "synthetic-secret" not in json.dumps(caught.value.details)

    missing = tmp_path / "missing-codex"
    with pytest.raises(V2Error) as unavailable:
        CodexReasoner(executable=str(missing)).complete("prompt", SCHEMA, timeout=1)
    assert unavailable.value.code == "unsupported-operation"


@pytest.mark.parametrize("mode", ["schema-stderr", "schema-message", "schema-event", "schema-zero"])
def test_codex_schema_rejection_reports_fixed_actionable_error(tmp_path: Path, monkeypatch, mode: str) -> None:
    script, _, _ = _stub(tmp_path)
    monkeypatch.setenv("SYNAPSE_TEST_STUB_MODE", mode)

    with pytest.raises(V2Error) as caught:
        CodexReasoner(executable=str(script)).complete("synthetic prompt", SCHEMA, timeout=3)

    error = caught.value
    assert error.code == "invalid-request"
    assert "invalid_json_schema" in str(error)
    assert "Update the Synapse adapter" in str(error)
    assert "sign-in" not in str(error)
    assert error.details == {"exit_status": 0 if mode == "schema-zero" else 1, "reason": "invalid_json_schema"}
    assert "synthetic-secret" not in json.dumps(error.to_dict())


def test_preparation_transport_schema_and_exact_evidence_mapping(tmp_path: Path, monkeypatch) -> None:
    script, _, _ = _stub(tmp_path)
    monkeypatch.setenv("SYNAPSE_TEST_STUB_MODE", "prepare")
    anchors = [{"source_id": "synthetic-first"}, {"source_id": "synthetic-second"}]
    state = {"sources": [{"evidence": anchor} for anchor in anchors]}
    reasoner = CodexReasoner(executable=str(script))

    result = reasoner.prepare(state)

    assert result["items"][0]["evidence"] == anchors[::-1]
    assert "evidence_indices" not in result["items"][0]
    result["items"][0]["evidence"][0]["source_id"] = "changed"
    assert anchors[1]["source_id"] == "synthetic-second"

    # The supported schema admits duplicates; local validation must reject them.
    monkeypatch.setenv("SYNAPSE_TEST_INDICES", "[0, 0]")
    with pytest.raises(V2Error, match="duplicate evidence"):
        reasoner.prepare(state)


@pytest.mark.parametrize(
    ("indices", "message"),
    [([], "non-empty"), (None, "non-empty"), ("0", "non-empty"),
     ([0, 0], "duplicate"), ([-1], "unknown"), ([2], "unknown"),
     ([True], "unknown"), ([0.0], "unknown"), (["0"], "unknown"), ([[]], "unknown")],
)
def test_preparation_locally_validates_evidence_indices(monkeypatch, indices, message: str) -> None:
    reasoner = CodexReasoner(executable="synthetic-not-started")
    monkeypatch.setattr(reasoner, "complete", lambda *_args, **_kwargs: {"items": [{"evidence_indices": indices}]})

    with pytest.raises(V2Error, match=message) as caught:
        reasoner.prepare({"sources": [{"evidence": {"source_id": "first"}}, {"evidence": {"source_id": "second"}}]})

    assert caught.value.code == "invalid-request"


def test_host_and_read_help_paths_do_not_start_codex(tmp_path: Path, monkeypatch) -> None:
    publisher, _fixture_host, _ = _bootstrap(tmp_path)
    started = []

    def fail_start(*_args, **_kwargs):
        started.append(True)
        raise AssertionError("ordinary host construction/read must not start Codex")

    monkeypatch.setattr("synapse.codex_host.subprocess.Popen", fail_start)
    reasoner = CodexReasoner(executable="synthetic-not-started")
    host = NativeHost(
        publisher.store.vault,
        event_reader=lambda reference: {"id": reference, "actor": "user", "text": "unused"},
        display=lambda _brief: None,
        reasoner=reasoner,
    )
    assert Gateway(publisher.store.vault).describe()["authority"]
    assert host.reasoner is reasoner
    assert started == []


@pytest.mark.parametrize("cancel_kind", ["cancel", "timeout"])
def test_codex_transport_terminates_process_group_without_orphan(
    tmp_path: Path, monkeypatch, cancel_kind: str
) -> None:
    script, log_path, child_path = _stub(tmp_path, mode="hang")
    monkeypatch.setenv("SYNAPSE_TEST_STUB_LOG", str(log_path))
    monkeypatch.setenv("SYNAPSE_TEST_STUB_MODE", "hang")
    monkeypatch.setenv("SYNAPSE_TEST_STUB_CHILD", str(child_path))
    reasoner = CodexReasoner(executable=str(script))

    if cancel_kind == "cancel":
        def cancelled() -> bool:
            return child_path.exists()

        expected = "cancelled"
        timeout = 3
    else:
        def cancelled() -> bool:
            return False

        expected = "coverage-limited"
        timeout = 0.3
    with pytest.raises(V2Error) as caught:
        reasoner.complete("prompt", SCHEMA, timeout=timeout, cancelled=cancelled)
    assert caught.value.code == expected
    pid = int(child_path.read_text(encoding="ascii"))
    _assert_dead(pid)
