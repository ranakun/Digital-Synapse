from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from synapse.blind_eval import (
    ALLOWED_TOOLS,
    _entity_ids,
    _hook_decision,
    assert_codex_isolation,
    assert_isolated_payload,
    build_codex_command,
    call_synapse_tool,
    initial_prompt,
    parse_codex_jsonl,
    run_isolated_suite,
)
from synapse.config import init_vault
from synapse.index import reindex

ENTITY_ID = "01J00000000000000000000001"


def test_owner_id_requires_explicit_citation_syntax() -> None:
    assert _entity_ids("Owner [me]") == {"me"}
    assert _entity_ids("Tell me about it") == set()


def _event(item: dict[str, object]) -> str:
    return json.dumps({"type": "item.completed", "item": item})


def test_initial_payload_excludes_all_hidden_question_metadata() -> None:
    question = {
        "id": "secret-id",
        "question": "Who should I approach?",
        "params": {"ref": "SECRET-PARAM-REF"},
        "expect": {"all_ids": ["SECRET-EXPECTED-ANSWER"]},
        "eval_note": "SECRET-RUBRIC-ANSWER",
        "cohort": "freshness",
    }

    assert_isolated_payload(question)
    payload = initial_prompt(question["question"])
    assert all(
        secret not in payload
        for secret in (
            "secret-id",
            "SECRET-PARAM-REF",
            "SECRET-EXPECTED-ANSWER",
            "SECRET-RUBRIC-ANSWER",
        )
    )
    assert "approved sets" in payload
    assert "distinctive question terms" in payload
    assert "newest relevant insight" in payload
    assert "college-linked or alumni" in payload
    assert "reserve the final call" in payload
    assert "warm-path" in payload
    assert "opportunity records a lead" in payload
    assert "cite `me` if it appears" in payload
    assert "search people for recommendation or testimonial" in payload
    assert "search projects and brief the exact project" in payload
    assert "satisfies every explicit constraint" in payload


def test_codex_command_is_ephemeral_capped_and_api_key_free(tmp_path: Path) -> None:
    command = build_codex_command(
        tmp_path / "vault",
        tmp_path / "workspace",
        "candidate prompt",
        codex="codex",
        hook_audit=tmp_path / "hook-audit.jsonl",
    )
    rendered = "\n".join(command)

    assert command[-1] == "candidate prompt"
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--dangerously-bypass-hook-trust" in command
    assert "gpt-5.6-sol" in command
    assert "model_reasoning_effort='medium'" in command
    assert "web_search='disabled'" in command
    assert "features.multi_agent=false" in command
    assert "hooks.PreToolUse" in rendered
    assert "--audit-file" in rendered
    assert "mcp_servers.synapse" in rendered
    assert "default_tools_approval_mode = 'approve'" in rendered
    assert all(name in rendered for name in ALLOWED_TOOLS)
    assert "API_KEY" not in rendered
    assert "questions.yaml" not in rendered


def test_jsonl_requires_synapse_trace_and_rejects_other_tools() -> None:
    valid = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            _event(
                {
                    "type": "mcp_tool_call",
                    "tool": "synapse_search",
                    "arguments": {"query": "target"},
                    "result": f"- Example Person `{ENTITY_ID}`",
                    "status": "completed",
                }
            ),
            _event(
                {
                    "type": "agent_message",
                    "text": f"1. Example Person `{ENTITY_ID}` - supported by search.",
                }
            ),
        ]
    )
    result = parse_codex_jsonl(valid)
    assert result["structural_pass"] is True
    assert result["codex_thread_id"] == "thread-1"
    assert len(result["tool_trace"]) == 1

    failed = parse_codex_jsonl(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.failed",
                        "item": {
                            "type": "mcp_tool_call",
                            "tool": "synapse_search",
                            "error": "timeout",
                        },
                    }
                ),
                _event({"type": "agent_message", "text": "No result."}),
            ]
        ),
        [{"tool_name": "mcp__synapse__synapse_search", "decision": "allow"}],
    )
    assert failed["retrieval_degraded"] is True
    assert failed["tool_trace"][0]["status"] == "failed"

    missing_terminal = parse_codex_jsonl(
        _event({"type": "agent_message", "text": f"Example `{ENTITY_ID}`"}),
        [{"tool_name": "mcp__synapse__synapse_search", "decision": "allow"}],
    )
    assert any("hook/trace mismatch" in item for item in missing_terminal["structural_failures"])

    cheated = "\n".join(
        [
            _event(
                {
                    "type": "command_execution",
                    "command": "Get-Content hidden-answer.txt",
                    "status": "completed",
                    "exit_code": 0,
                }
            ),
            _event({"type": "agent_message", "text": f"Example `{ENTITY_ID}`"}),
        ]
    )
    result = parse_codex_jsonl(cheated)
    assert result["structural_pass"] is False
    assert any("forbidden tool activity" in item for item in result["structural_failures"])
    assert any("absent from its tool trace" in item for item in result["structural_failures"])

    hook_blocked = parse_codex_jsonl(
        valid,
        [{"tool_name": "Bash", "decision": "deny"}],
    )
    assert hook_blocked["structural_pass"] is False
    assert any(
        "blocked non-Synapse tool attempts" in item
        for item in hook_blocked["structural_failures"]
    )


def test_pretool_hook_denies_everything_except_eval_mcp() -> None:
    assert _hook_decision("mcp__synapse__synapse_dossier") is None
    assert _hook_decision("synapse_search")["hookSpecificOutput"]["permissionDecision"] == "deny"
    decision = _hook_decision("Bash")
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_eval_search_is_text_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = init_vault(tmp_path / "vault", initialize_git=False)
    seen: dict[str, object] = {}

    def fake_search(*_args: object, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"results": []}

    monkeypatch.setattr("synapse.blind_eval.hybrid_search", fake_search)
    assert call_synapse_tool(vault, "synapse_search", {"query": "recruiter"}) == "(no results)"
    assert seen["text_only"] is True
    assert seen["entity_type"] is None


def test_eval_search_surfaces_opportunity_outcome_metadata(tmp_path: Path) -> None:
    vault = init_vault(tmp_path / "vault", initialize_git=False)
    opportunities = vault / "entities" / "opportunities"
    opportunities.mkdir(parents=True, exist_ok=True)
    (opportunities / "hired-role.md").write_text(
        f"""---
id: {ENTITY_ID}
type: opportunity
name: Research Engineer at Example
review_status: proposed
tags: []
relations: []
properties:
  role: Research Engineer
  stage: hired
  company: Example
  contacted_on: '2024-02-19'
---

Research Engineer opportunity.
""",
        encoding="utf-8",
    )
    reindex(vault)

    result = call_synapse_tool(
        vault,
        "synapse_search",
        {"query": "Research Engineer", "type": "opportunity"},
    )

    assert "Research Engineer role" in result
    assert "hired stage" in result
    assert "Example company" in result
    assert "2024-02-19 contacted_on" in result


def test_isolation_canary_requires_a_blocked_command(tmp_path: Path) -> None:
    def blocked_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "\n".join(
            [
                _event(
                    {
                        "type": "command_execution",
                        "status": "failed",
                        "exit_code": 1,
                    }
                ),
                _event({"type": "agent_message", "text": "ACCESS_DENIED"}),
            ]
        )
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    evidence = assert_codex_isolation(tmp_path, codex="codex", run=blocked_run)
    assert evidence["passed"] is True
    assert evidence["shell_attempts"] == 1
    assert evidence["denied_shell_events"] == 0
    assert evidence["hook_trace_sha256"]
    assert evidence["answer_sha256"]

    def successful_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "\n".join(
            [
                _event(
                    {
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                    }
                ),
                _event({"type": "agent_message", "text": "ACCESS_DENIED"}),
            ]
        )
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    with pytest.raises(RuntimeError, match="canary failed"):
        assert_codex_isolation(tmp_path, codex="codex", run=successful_run)


def test_parallel_suite_records_one_candidate_failure_without_losing_others(
    tmp_path: Path,
) -> None:
    vault = init_vault(tmp_path / "vault", initialize_git=False)
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        """- id: first
  question: First synthetic question
  via: search
- id: second
  question: Second synthetic question
  via: search
""",
        encoding="utf-8",
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        prompt = command[-1]
        audit = Path(str(_kwargs["cwd"])) / "hook-audit.jsonl"
        if "answer-key-canary" in prompt:
            audit.write_text(
                json.dumps({"tool_name": "Bash", "decision": "deny"}) + "\n",
                encoding="utf-8",
            )
            stdout = "\n".join(
                [
                    _event({"type": "command_execution", "status": "failed", "exit_code": 1}),
                    _event({"type": "agent_message", "text": "ACCESS_DENIED"}),
                ]
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if "Second synthetic" in prompt:
            raise RuntimeError("synthetic candidate failure")
        audit.write_text(
            json.dumps(
                {
                    "tool_name": "mcp__synapse__synapse_search",
                    "decision": "allow",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stdout = "\n".join(
            [
                _event(
                    {
                        "type": "mcp_tool_call",
                        "tool": "synapse_search",
                        "status": "completed",
                        "result": "Owner `me`",
                    }
                ),
                _event({"type": "agent_message", "text": "Owner `me`"}),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    output, summary = run_isolated_suite(
        vault,
        questions,
        workers=2,
        codex="codex",
        run=fake_run,
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary == {
        "total": 2,
        "structural_pass": 1,
        "anchor_scored": 0,
        "anchor_pass": 0,
        "process_failures": 1,
        "retrieval_degraded": 1,
    }
    assert {row["id"] for row in rows} == {"first", "second"}
    manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["canary_passed"] is True
    assert manifest["canary"]["passed"] is True
    assert manifest["search_mode"] == "text-only"
    assert manifest["question_file_stable"] is True
    assert manifest["index_fingerprint_kind"] == "logical-table-content-v1"
    assert manifest["index_stable"] is True
    assert manifest["evaluator_stable"] is True
    assert manifest["attestation_passed"] is True
    assert manifest["attestation_failure"] is None
    assert manifest["output_sha256"]
    assert manifest["evaluator_sha256"]
    assert manifest["system_prompt_sha256"]
    assert manifest["summary"] == summary
