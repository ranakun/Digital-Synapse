"""Synthetic integration checks for installed owner-knowledge read interfaces."""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import date

import pytest
from typer.testing import CliRunner

from synapse import blind_eval, mcpserver, owner_context
from synapse.brief import build_entity_brief
from synapse.cli import app
from synapse.evals import load_questions, run_question
from synapse.guide import generate_agent_guide
from synapse.index import connect, reindex
from synapse.owner_context import build_owner_context
from synapse.proposals import apply_proposal
from synapse.search import hybrid_search
from synapse.util import write_frontmatter

NAV = "01J00000000000000000000700"
FINDING = "01J00000000000000000000701"
EVIDENCE = "01J00000000000000000000702"
CORRECTION = "01J00000000000000000000703"
LEGACY = "01J00000000000000000000704"
PROJECT = "01J00000000000000000000705"
AMBIGUOUS = "01J00000000000000000000706"
PREFERENCE = "01J00000000000000000000707"
AS_OF = "2026-09-15"
runner = CliRunner()


def _body(statement, limits, evidence="Captured synthetic source, span U01."):
    return f"## Statement\n{statement}\n\n## Conditions and limits\n{limits}\n\n## Evidence\n{evidence}\n"


def _relation(target, roles, **properties):
    return {
        "type": "related_to", "target": target,
        "properties": {"roles": roles, "note": "Synthetic recorded connection.", **properties},
    }


def _profile(key, kind="finding", **extra):
    return {
        "knowledge_profile": "owner-knowledge-v1", "subject_id": "me", "record_kind": kind,
        "claim_key": key, "facets": ["learning"], "epistemic_basis": ["owner-report"],
        "owner_position": "stated", "lifecycle": "current", "as_of": "2026-09-01",
        "source_family_id": "synthetic-inquiry", **extra,
    }


def _write(vault, eid, name, body, *, properties=None, relations=None, aliases=None, etype="insight"):
    fm = {
        "id": eid, "type": etype, "name": name, "review_status": "proposed",
        "properties": properties or {}, "relations": relations or [], "aliases": aliases or [],
    }
    write_frontmatter(vault / "entities" / "insights" / f"{eid}.md", fm, body)


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    path = tmp_path / "vault"
    monkeypatch.setattr(owner_context, "_today", lambda: date.fromisoformat(AS_OF))
    _write(path, "me", "Synthetic Owner", "Owner.", etype="person")
    _write(
        path, NAV, "Owner knowledge navigation", _body("Recorded knowledge collection.", "Sources retain their separate basis."),
        properties=_profile("owner.navigation", "navigation", facets=["discovery"]),
        relations=[_relation("me", ["about"])],
    )
    _write(
        path, FINDING, "Representation capability", _body("CAPABILITY: useful representations in recorded tasks.", "CAPABILITY LIMIT: this is not a measured IQ or learning rate."),
        properties=_profile("zzz.capability", context_order=10),
        relations=[_relation(NAV, ["member"]), _relation(EVIDENCE, ["evidence"]), _relation(PROJECT, ["applies_to"])],
    )
    _write(
        path, EVIDENCE, "Reasoning episode", _body("A synthetic answer used an explicit model.", "Assistance was recorded separately.", "VERBATIM EVIDENCE: original answer and assistance at R03."),
        properties=_profile("episode.reasoning", "evidence", lifecycle="historical"),
        relations=[_relation(NAV, ["member"])], etype="conversation",
    )
    _write(
        path, CORRECTION, "Refinement qualification", _body("CORRECTION: new feedback changed a tentative decision.", "CORRECTION LIMIT: the separate continuity account remains unresolved."),
        properties=_profile("refinement.qualification", facets=["quality", "work"]),
        relations=[
            _relation(NAV, ["member"]),
            _relation(LEGACY, ["qualifies"], scope="One refinement example"),
            _relation(FINDING, ["qualifies"], scope="One application of the capability"),
        ],
    )
    _write(path, LEGACY, "Temporal pattern archive", "LEGACY CLAIM: an older broad account.", properties={"as_of": "2026-07-27"})
    _write(path, PROJECT, "Selected project", "The project's actual acceptance criteria.", aliases=["project-alias", "shared-target"], etype="project")
    _write(path, AMBIGUOUS, "Another project", "Unrelated project.", aliases=["shared-target"], etype="project")
    _write(
        path, PREFERENCE, "Shared collaboration preference", _body("COLLABORATION: explain the purpose first.", "This is a preference for this kind of inquiry."),
        properties=_profile("aaa.collaboration", "preference", facets=["collaboration"], context_order=90),
        relations=[_relation(NAV, ["member"])],
    )
    assert not reindex(path, full=True).has_errors
    return path


@pytest.fixture()
def connection(vault, monkeypatch):
    conn = connect(vault)
    monkeypatch.setattr(mcpserver, "get_connection", lambda: conn)
    monkeypatch.setattr(mcpserver, "_vault_path", vault)
    monkeypatch.setattr(mcpserver, "_search_runtime", None)

    def local_search(*args, **kwargs):
        kwargs["text_only"] = True
        return hybrid_search(*args, **kwargs)

    monkeypatch.setattr(mcpserver, "hybrid_search", local_search)
    yield conn
    conn.close()


def test_cli_matches_core_and_resolves_alias(vault, connection):
    expected = build_owner_context(connection, facet="learning", target_id=PROJECT, as_of=AS_OF, budget_chars=8000)
    result = runner.invoke(app, ["owner-context", "--facet", "learning", "--target", "project-alias", "--as-of", AS_OF, "--budget", "8000", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip() == expected.rstrip()
    assert "CAPABILITY" in result.stdout and "CAPABILITY LIMIT" in result.stdout
    assert "CORRECTION LIMIT" in result.stdout


@pytest.mark.parametrize("arguments,exit_code,expected", [
    (["--facet", "unrecorded-topic"], 2, "Unknown facet"),
    (["--as-of", "2026-02-30"], 2, "calendar"),
    (["--budget", "0"], 2, "Invalid value"),
    (["--target", "missing-target"], 1, "Target must identify one entity"),
    (["--target", "shared-target"], 1, "Selected project"),
])
def test_cli_invalid_input_and_ambiguity_are_explicit(vault, arguments, exit_code, expected):
    result = runner.invoke(app, ["owner-context", *arguments, "--vault", str(vault)])
    assert result.exit_code == exit_code
    assert expected in result.output
    assert "CAPABILITY:" not in result.output


def test_mcp_and_blind_allowed_read_match_core(vault, connection):
    args = {"facet": "learning", "target": "project-alias", "as_of": AS_OF, "budget": 7000}
    expected = build_owner_context(connection, facet="learning", target_id=PROJECT, as_of=AS_OF, budget_chars=7000)
    assert mcpserver._synapse_owner_context_sync(**args) == expected
    assert "synapse_owner_context" in blind_eval.ALLOWED_TOOLS
    assert blind_eval.call_synapse_tool(vault, "synapse_owner_context", args) == expected
    assert "Tool not allowed" in blind_eval.call_synapse_tool(vault, "synapse_verify", {})


@pytest.mark.parametrize("args, expected", [
    ({"facet": "unrecorded-topic"}, "Unknown facet"),
    ({"as_of": "yesterday"}, "YYYY-MM-DD"),
    ({"target": "shared-target"}, "Selected project"),
    ({"budget": 0}, "budget must"),
    ({"budget": 32001}, "budget must"),
])
def test_mcp_and_blind_bad_context_arguments_do_not_return_claims(vault, connection, args, expected):
    for output in (
        mcpserver._synapse_owner_context_sync(**args),
        blind_eval.call_synapse_tool(vault, "synapse_owner_context", args),
    ):
        assert expected in output
        assert "CAPABILITY:" not in output


@pytest.mark.parametrize("budget", [500, 2000, 6000])
def test_mcp_profile_entity_and_brief_preserve_limits(connection, budget):
    for output in (
        mcpserver._synapse_entity_sync(FINDING, budget=budget),
        mcpserver._synapse_brief_sync(FINDING, budget=budget),
    ):
        assert len(output) <= budget
        assert ("CAPABILITY:" in output) == ("CAPABILITY LIMIT:" in output)
        assert ("CORRECTION:" in output) == ("CORRECTION LIMIT:" in output)
        if "CAPABILITY:" in output:
            assert "CORRECTION LIMIT:" in output
            assert f"`{CORRECTION}`" in output


def test_legacy_brief_and_entity_expose_correction_before_old_body(vault, connection):
    outputs = [
        build_entity_brief(connection, LEGACY, budget_tokens=750),
        mcpserver._synapse_entity_sync(LEGACY, budget=3000),
        mcpserver._synapse_brief_sync(LEGACY, budget=3000),
        blind_eval.call_synapse_tool(vault, "synapse_brief", {"ref": LEGACY}),
    ]
    for output in outputs:
        assert "Qualified/revised" in output
        assert f"`{CORRECTION}`" in output
        if "LEGACY CLAIM:" in output:
            assert output.index(f"`{CORRECTION}`") < output.index("LEGACY CLAIM:")
    assert all(len(output) <= 3000 for output in outputs[:3])


def test_cli_typed_search_and_mcp_search_include_scoped_notice(vault, connection):
    result = runner.invoke(app, ["search", "Temporal pattern archive", "--text-only", "--type", "insight", "--json", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    hit = next(item for item in payload["results"] if item["id"] == LEGACY)
    assert "Qualified/revised" in hit["knowledge_notice"]
    assert f"`{CORRECTION}`" in hit["knowledge_notice"]
    assert all(item["type"] == "insight" for item in payload["results"])
    mcp = mcpserver._synapse_search_sync("Temporal pattern archive", limit=5, type="insight")
    assert f"`{LEGACY}`" in mcp and f"`{CORRECTION}`" in mcp
    assert "Qualified/revised" in mcp
    assert len(mcp) <= 2500


def test_generated_guide_and_owner_briefs_teach_context_route(vault, connection):
    guide = generate_agent_guide(connection, vault)
    readable = " ".join(guide.split())
    for phrase in ("synapse_owner_context", "--facet learning", "--target", "source basis", "do not become independent evidence"):
        assert phrase in readable
    implicit = runner.invoke(app, ["brief", "--vault", str(vault)])
    explicit = runner.invoke(app, ["brief", "me", "--vault", str(vault)])
    assert implicit.exit_code == explicit.exit_code == 0
    assert implicit.stdout == explicit.stdout
    assert "owner-context" in explicit.stdout
    assert "owner-context" in mcpserver._synapse_brief_sync()


def test_authored_order_reaches_context_interfaces(vault, connection):
    cli = runner.invoke(app, ["owner-context", "--as-of", AS_OF, "--budget", "8000", "--vault", str(vault)])
    assert cli.exit_code == 0
    output = cli.stdout
    assert "CAPABILITY:" in output and "COLLABORATION:" in output
    assert output.index("CAPABILITY:") < output.index("COLLABORATION:")


def test_blind_search_never_cuts_a_correction_between_statement_and_limit(vault, connection, monkeypatch):
    # Exercise the blind formatter's outer cap, which is separate from each
    # individual notice's cap. Every search hit is a unique indexed legacy record.
    results = []
    for number in range(24):
        old_id = f"01J000000000000000000008{number:02d}"
        correction_id = f"01J000000000000000000009{number:02d}"
        _write(vault, old_id, f"Archive {number}", f"Historical result {number}.")
        _write(
            vault, correction_id, f"Correction {number}",
            _body(f"STMT_{number:02d}: a scoped observation.", f"LIMIT_{number:02d}: no comparative conclusion follows."),
            properties=_profile(f"correction.{number}", source_family_id="one-common-source"),
            relations=[_relation(NAV, ["member"]), _relation(old_id, ["qualifies"], scope="One observation")],
        )
        results.append({"id": old_id, "name": f"Archive {number}", "type": "insight", "snippet": "A historical record. " * 4})
    reindex(vault, full=True)
    monkeypatch.setattr(blind_eval, "hybrid_search", lambda *args, **kwargs: {"results": results})
    violations = []
    lengths = []
    for snippet_length in (0, 17, 39, 67, 91):
        for item in results:
            item["snippet"] = "x" * snippet_length
        text = blind_eval.call_synapse_tool(vault, "synapse_search", {"query": "archive", "limit": 25})
        lengths.append(len(text))
        for number in range(24):
            if (f"STMT_{number:02d}:" in text) != (f"LIMIT_{number:02d}:" in text):
                violations.append((snippet_length, number, text[-300:]))
    assert violations == []
    assert max(lengths) <= 8000


def test_deterministic_eval_uses_only_rendered_context_and_citations(vault, connection, tmp_path):
    question = {
        "id": "synthetic-owner-context",
        "question": "What does the recorded capability support for this project?",
        "via": "owner-context",
        "params": {"facet": "learning", "target": "project-alias", "as_of": AS_OF, "budget": 8000},
        "expect": {
            "all_ids": [FINDING, CORRECTION],
            "text": ["CAPABILITY:", "CAPABILITY LIMIT:", "CORRECTION LIMIT:"],
            "forbid": ["VERBATIM EVIDENCE:"],
        },
    }
    # This is a new synthetic file, not the owner's evaluation bank.
    questions_path = tmp_path / "synthetic-owner-context-questions.yaml"
    questions_path.write_text(json.dumps([question]), encoding="utf-8")
    loaded = load_questions(questions_path)
    assert loaded == [question]
    result = run_question(connection, vault, loaded[0])
    assert result.passed, result.error or result.details

    # Linked evidence being indexed must not let invisible body text satisfy an
    # output expectation; likewise an unrelated indexed ID must not count.
    for expectation in ({"text": "VERBATIM EVIDENCE:"}, {"all_ids": [AMBIGUOUS]}):
        result = run_question(connection, vault, {**question, "expect": expectation})
        assert not result.passed
        assert result.error is None
        assert result.details

    for target in ("missing-target", "shared-target"):
        result = run_question(connection, vault, {
            **question, "params": {**question["params"], "target": target},
        })
        assert not result.passed
        assert "exactly one entity" in result.error


@pytest.mark.parametrize("order", [0, -1, True, 1.5, "1"])
def test_presentation_order_rejects_nonpositive_or_noninteger_values(order):
    body = _body("A scoped observation.", "No numeric ability conclusion follows.")
    errors = owner_context.validate_knowledge_record({
        "type": "insight", "properties": _profile("ordering.validation", context_order=order),
    }, body)
    assert any("context_order" in error for error in errors)


def test_mixed_source_families_and_positive_order_survive_indexed_read(vault, connection):
    props = _profile(
        "zzz.capability", context_order=1,
        source_family_ids=["owner-session", "canonical-context"],
        source_refs=["Owner transcript U10-U14", "Canonical snapshot, section X"],
    )
    del props["source_family_id"]
    body = _body("CAPABILITY: useful representations in recorded tasks.", "CAPABILITY LIMIT: this is not a measured IQ or learning rate.")
    assert owner_context.validate_knowledge_record({"type": "insight", "properties": props}, body) == []
    _write(
        vault, FINDING, "Representation capability", body, properties=props,
        relations=[_relation(NAV, ["member"]), _relation(EVIDENCE, ["evidence"]), _relation(PROJECT, ["applies_to"])],
    )
    assert not reindex(vault, full=True).has_errors
    for output in (
        mcpserver._synapse_owner_context_sync(facet="learning", as_of=AS_OF),
        mcpserver._synapse_entity_sync(FINDING, budget=6000),
    ):
        assert "`owner-session`" in output and "`canonical-context`" in output
        assert "CAPABILITY:" in output and "CAPABILITY LIMIT:" in output
        assert "CORRECTION LIMIT:" in output


@pytest.mark.parametrize("field", ["source_family_ids", "source_refs"])
@pytest.mark.parametrize("value", ["single-unstructured-value", [], ["one", ""], ["one", 2]])
def test_mixed_source_metadata_requires_nonempty_flat_string_lists(field, value):
    errors = owner_context.validate_knowledge_record({
        "type": "insight", "properties": _profile("source.validation", **{field: value}),
    }, _body("A scoped observation.", "Sources are not independent confirmations."))
    assert any(field in error for error in errors)


def test_executing_synthetic_capture_needs_no_subprocess(vault, connection, monkeypatch, capsys):
    calls = []

    def forbidden_process(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Applying a knowledge proposal must not launch a process")

    monkeypatch.setattr(subprocess, "run", forbidden_process)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr("synapse.maintenance.nearest_duplicates", lambda *args, **kwargs: [])
    proposal = {
        "proposal": "01J00000000000000000000708", "agent": "synthetic-surface-test",
        "created_at": "2026-09-10", "rationale": "Verify the local capture boundary.",
        "confidence": "high",
        "base": [dict(row) for row in connection.execute("SELECT id, content_hash FROM entities")],
        "ops": [{
            "op": "create_entity", "type": "insight", "name": "Applied synthetic finding",
            "properties": _profile("applied.surface"),
            "body": _body("APPLIED CLAIM: a recorded observation.", "APPLIED LIMIT: no broader inference follows."),
            "relations": [_relation(NAV, ["member"]), _relation(EVIDENCE, ["evidence"])],
        }],
    }
    path = vault / "proposals" / "pending" / "synthetic-apply.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal), encoding="utf-8")
    result = apply_proposal(vault, path, execute=True)
    assert result == {"success": True, "results": ["applied"]}
    assert calls == []
    assert not path.exists()
    assert (vault / "proposals" / "applied" / path.name).exists()
    assert "Applied proposal: 1 change(s), 0 already present." in capsys.readouterr().out

    row = connection.execute("SELECT id, frontmatter FROM entities WHERE name = ?", ("Applied synthetic finding",)).fetchone()
    assert json.loads(row["frontmatter"])["review_status"] == "proposed"
    output = mcpserver._synapse_entity_sync(row["id"], budget=6000)
    assert "APPLIED CLAIM:" in output and "APPLIED LIMIT:" in output


def test_blind_mcp_and_hook_keep_virtual_environment_interpreter_symlink(tmp_path, monkeypatch):
    base_python = tmp_path / "base-interpreter" / "python"
    base_python.parent.mkdir()
    base_python.write_text("Synthetic interpreter placeholder; never executed.", encoding="utf-8")
    venv_python = tmp_path / "virtual environment with spaces" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    try:
        venv_python.symlink_to(base_python)
    except OSError as exc:
        pytest.skip(f"Creating interpreter symlinks is unavailable: {exc}")
    assert venv_python.resolve() == base_python.resolve()
    assert venv_python.absolute() != venv_python.resolve()
    monkeypatch.setattr(blind_eval.sys, "executable", str(venv_python))

    command = blind_eval.build_codex_command(
        tmp_path / "synthetic-vault", tmp_path / "workspace", "Synthetic prompt.",
        codex="not-launched", hook_audit=tmp_path / "audit.jsonl",
    )
    mcp_config = tomllib.loads(next(part for part in command if part.startswith("mcp_servers.synapse=")))
    hook_config = tomllib.loads(next(part for part in command if part.startswith("hooks.PreToolUse=")))
    expected = str(venv_python.absolute())
    assert mcp_config["mcp_servers"]["synapse"]["command"] == expected
    hook_command = hook_config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook_command.startswith((f'"{expected}" ', f"'{expected}' ", f"{expected} "))
    assert "-m synapse.blind_eval hook" in hook_command
    assert str(base_python.resolve()) not in hook_command


def test_umbrella_order_and_concrete_facet_priority_survive_tight_budget(tmp_path):
    path = tmp_path / "ranking-vault"
    _write(path, "me", "Synthetic Owner", "Owner.", etype="person")
    _write(
        path, NAV, "Owner knowledge navigation", _body("Recorded knowledge collection.", "All source limits apply."),
        properties=_profile("owner.navigation", "navigation", facets=["discovery"]),
        relations=[_relation("me", ["about"])],
    )
    padding = "The recorded context matters. " * 30
    quality_body = _body("QUALITY PRIORITY: " + padding, "QUALITY LIMIT: this applies to the specific review example.")
    _write(
        path, FINDING, "Earlier quality finding", quality_body,
        properties=_profile("quality.priority", facets=["quality"], context_order=10),
        relations=[_relation(NAV, ["member"])],
    )
    _write(
        path, CORRECTION, "Later literal work and planning finding",
        _body("LITERAL TOPIC: " + padding, "Literal topic tags do not imply greater importance."),
        properties=_profile("literal.topic", facets=["work", "planning"], context_order=20),
        relations=[_relation(NAV, ["member"])],
    )

    def write_shared_preference(order):
        _write(
            path, PREFERENCE, "Shared preference",
            _body("SHARED CORE: " + padding, "This preference is general to collaboration."),
            properties=_profile("shared.core", "preference", facets=["collaboration"], context_order=order),
            relations=[_relation(NAV, ["member"])],
        )

    write_shared_preference(30)
    assert not reindex(path, full=True).has_errors
    conn = connect(path)
    try:
        # Only one full finding fits. Umbrella selection must respect authored
        # order across constituent topics, even when another card has its literal tag.
        for facet in ("work", "planning"):
            output = build_owner_context(conn, facet=facet, as_of=AS_OF, budget_chars=2500)
            assert len(output) <= 2500
            assert "QUALITY PRIORITY:" in output and "QUALITY LIMIT:" in output
            assert "LITERAL TOPIC:" not in output and "SHARED CORE:" not in output
            assert "Omitted" in output

        # A concrete facet still takes priority over shared collaboration context,
        # even if the shared preference has an earlier presentation order.
        write_shared_preference(1)
        assert not reindex(path, full=True).has_errors
        output = build_owner_context(conn, facet="quality", as_of=AS_OF, budget_chars=2500)
        assert "QUALITY PRIORITY:" in output and "QUALITY LIMIT:" in output
        assert "SHARED CORE:" not in output
    finally:
        conn.close()


def test_blind_owner_bracket_citations_require_a_real_entity_id():
    for text in (f"`[me; {FINDING}]`", f"[me, {FINDING}]", f"[ me ; {FINDING} ]"):
        assert blind_eval._entity_ids(text) == {"me", FINDING}
    for text in (
        "Tell me about it", "[me; words]", "[me, words]",
        "[me; 01J0000000000000000000070]", f"me; {FINDING}",
    ):
        assert "me" not in blind_eval._entity_ids(text)
