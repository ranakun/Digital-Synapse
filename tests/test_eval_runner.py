from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from synapse.evals import (
    append_history,
    load_questions,
    resolve_merged_id,
    run_all,
    run_question,
)
from synapse.index import connect, reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
FIXTURE_QUESTIONS = Path(__file__).parent / "fixtures" / "evals" / "questions.yaml"


@pytest.fixture()
def temp_vault(tmp_path: Path, monkeypatch) -> Path:
    from synapse.embeddings import HashEmbedder

    # Runner accounting uses deterministic text results, with no model download.
    monkeypatch.setattr("synapse.search.default_embedder", lambda _: HashEmbedder())
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT,
        vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal"),
    )
    # Ensure history.jsonl is removed if copied
    hist_file = vault / "evals" / "history.jsonl"
    if hist_file.exists():
        hist_file.unlink()
    reindex(vault, full=True)
    return vault


def test_load_questions() -> None:
    questions = load_questions(FIXTURE_QUESTIONS)
    assert len(questions) >= 6
    assert questions[0]["id"] == "find-person"
    assert questions[0]["via"] == "find"

    # Test file not found
    with pytest.raises(FileNotFoundError):
        load_questions(Path("nonexistent_questions.yaml"))


def test_load_questions_invalid(tmp_path: Path) -> None:
    # Test invalid YAML list
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("invalid_yaml: yes", encoding="utf-8")
    with pytest.raises(ValueError, match="Questions file must be a YAML list"):
        load_questions(bad_yaml)

    # Test missing id
    bad_yaml.write_text("- question: Hello\n  via: find", encoding="utf-8")
    with pytest.raises(ValueError, match="missing an 'id'"):
        load_questions(bad_yaml)

    # Test invalid via
    bad_yaml.write_text("- id: test\n  question: Hello\n  via: invalid_op", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or missing 'via'"):
        load_questions(bad_yaml)


def test_run_question_find(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "find-person",
            "question": "Where is Example Person?",
            "via": "find",
            "params": {"text": "Example Person"},
            "expect": {
                "all_ids": ["01J00000000000000000000001"],
                "text": ["Singapore"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
        assert not res.error
        assert not res.warnings
    finally:
        conn.close()


def test_run_question_search(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        # We need mock/test semantic embedder for local test
        q = {
            "id": "search-company",
            "question": "Search for Example Company",
            "via": "search",
            "params": {"query": "Example Company", "text_only": True},
            "expect": {
                "any_ids": ["01J00000000000000000000002"],
                "text": ["vendor"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
    finally:
        conn.close()


def test_run_question_neighbors(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "neighbors-example",
            "question": "Neighbors of Example Person",
            "via": "neighbors",
            "params": {"start": "Example Person", "depth": 1},
            "expect": {
                "all_ids": ["01J00000000000000000000002"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
    finally:
        conn.close()


def test_run_question_path(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "path-example",
            "question": "Path between Example Person and Example Company",
            "via": "path",
            "params": {"start": "Example Person", "end": "Example Company"},
            "expect": {
                "all_ids": ["01J00000000000000000000001", "01J00000000000000000000002"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
    finally:
        conn.close()


def test_run_question_filter(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "filter-example",
            "question": "Filter recruiters",
            "via": "filter",
            "params": {"tag": "recruiter"},
            "expect": {
                "all_ids": ["01J00000000000000000000013"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
    finally:
        conn.close()


def test_run_question_brief(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "brief-example",
            "question": "Brief for Example Person",
            "via": "brief",
            "params": {"ref": "Example Person", "budget": 4000},
            "expect": {
                "text": ["CTO"],
                "forbid": ["ForbiddenWordThatDoesNotExist"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is True
    finally:
        conn.close()


def test_run_question_fail_deliberately(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        q = {
            "id": "fail-deliberately",
            "question": "This question should fail",
            "via": "find",
            "params": {"text": "NonexistentPersonOrCompany"},
            "expect": {
                "any_ids": ["01J00000000000000000000001"],
            },
        }
        res = run_question(conn, temp_vault, q)
        assert res.passed is False
        assert "None of the expected any_ids" in res.details
    finally:
        conn.close()


def test_resolve_merged_id(temp_vault: Path) -> None:
    conn = connect(temp_vault)
    try:
        # Create a mock merged entity frontmatter in SQLite directly for testing
        conn.execute(
            "INSERT INTO entities(id, type, name, file_path, frontmatter, content_hash) "
            "VALUES ('merged_id_1', 'person', 'Old Name', 'some_file.md', '{\"merged_into\": \"01J00000000000000000000001\"}', 'hash')"
        )
        conn.commit()

        res_id, warning = resolve_merged_id(conn, "merged_id_1")
        assert res_id == "01J00000000000000000000001"
        assert warning == "Warning: expected ID merged_id_1 has been merged into 01J00000000000000000000001"

        # Check normal non-merged ID
        res_id_2, warning_2 = resolve_merged_id(conn, "01J00000000000000000000001")
        assert res_id_2 == "01J00000000000000000000001"
        assert warning_2 is None
    finally:
        conn.close()


def test_run_all_and_history(temp_vault: Path) -> None:
    questions = load_questions(FIXTURE_QUESTIONS)
    results, summary = run_all(temp_vault, questions)

    assert len(results) == len(questions)
    assert summary["pass"] == len(questions) - 1  # only the deliberately failing one fails
    assert summary["fail"] == 1
    assert summary["ids_failed"] == ["fail-deliberately"]

    # Test append_history writes history file and appends runs
    append_history(temp_vault, summary)
    hist_file = temp_vault / "evals" / "history.jsonl"
    assert hist_file.exists()

    lines = hist_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# expectations are owner-maintained; agents must not edit questions to make evals pass."
    
    record = json.loads(lines[1])
    assert record["pass"] == len(questions) - 1
    assert record["fail"] == 1
    assert record["ids_failed"] == ["fail-deliberately"]

    # Append again
    append_history(temp_vault, summary)
    lines2 = hist_file.read_text(encoding="utf-8").splitlines()
    assert len(lines2) == 3


def test_cli_eval(temp_vault: Path) -> None:
    from typer.testing import CliRunner

    from synapse.cli import app

    runner = CliRunner()

    # 1. Test missing questions.yaml defaults to <vault>/evals/questions.yaml (which is missing in temp_vault initially)
    result = runner.invoke(app, ["eval", "--vault", str(temp_vault)])
    assert result.exit_code == 2
    assert "no questions authored yet" in result.output

    # 2. Test running with explicit questions file
    result2 = runner.invoke(app, ["eval", "--questions", str(FIXTURE_QUESTIONS), "--vault", str(temp_vault)])
    # Since it has a deliberately failing question, it should exit with code 1
    assert result2.exit_code == 1
    assert "Evaluation Results" in result2.output
    assert "fail-deliberately    | FAIL" in result2.output

    # 3. Test running with json option
    result3 = runner.invoke(app, ["eval", "--questions", str(FIXTURE_QUESTIONS), "--vault", str(temp_vault), "--json"])
    assert result3.exit_code == 1
    
    # Extract only the JSON portion from the output
    output = result3.output
    start_idx = output.find('{')
    end_idx = output.rfind('}')
    assert start_idx != -1 and end_idx != -1, f"Failed to find JSON bounds in: {output}"
    json_str = output[start_idx:end_idx+1]
    
    data = json.loads(json_str)
    assert "results" in data
    assert "summary" in data
    assert data["summary"]["fail"] == 1
