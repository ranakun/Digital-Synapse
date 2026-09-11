from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.cli import app
from synapse.index import reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture()
def temp_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal"))
    log_file = vault / ".synapse" / "query-log.jsonl"
    if log_file.exists():
        log_file.unlink()
    reindex(vault, full=True)
    return vault


def test_querylog_creation_and_contents(temp_vault: Path) -> None:
    runner = CliRunner()
    
    # 1. Run a find command that matches something
    result = runner.invoke(app, ["find", "Example", "--vault", str(temp_vault)])
    assert result.exit_code == 0
    
    # Assert query log line was written
    log_file = temp_vault / ".synapse" / "query-log.jsonl"
    assert log_file.exists()
    
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    
    record = json.loads(lines[0])
    assert record["iface"] == "cli"
    assert record["op"] == "find"
    assert record["params"] == {"text": "Example"}
    assert record["result_count"] > 0
    assert record["zero_hit"] is False
    assert "ts" in record
    assert "duration_ms" in record
    
    # 2. Run a find command with a zero hit
    result = runner.invoke(app, ["find", "nonexistent_term_xyz", "--vault", str(temp_vault)])
    assert result.exit_code == 0
    
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    
    record_zero = json.loads(lines[1])
    assert record_zero["op"] == "find"
    assert record_zero["params"] == {"text": "nonexistent_term_xyz"}
    assert record_zero["result_count"] == 0
    assert record_zero["zero_hit"] is True


def test_query_report_and_corrupt_line(temp_vault: Path) -> None:
    runner = CliRunner()
    
    # Trigger log creation
    _ = runner.invoke(app, ["find", "Example", "--vault", str(temp_vault)])
    
    log_file = temp_vault / ".synapse" / "query-log.jsonl"
    assert log_file.exists()
    
    # Write a corrupt/invalid line and a valid zero-hit line manually
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("{invalid json line\n")
        f.write(json.dumps({
            "ts": "2026-06-11T12:00:00Z",
            "iface": "cli",
            "op": "find",
            "params": {"text": "zero_hit_query_abc"},
            "result_count": 0,
            "duration_ms": 15,
            "zero_hit": True,
            "fallback": "substring"
        }) + "\n")
        
    # Run report command and verify it aggregates correctly and doesn't crash
    result = runner.invoke(app, ["query-report", "--vault", str(temp_vault)])
    assert result.exit_code == 0
    assert "Query Log Report" in result.output
    assert "Operation Volume:" in result.output
    assert "Top Zero-Hit Queries:" in result.output
    assert "zero_hit_query_abc" in result.output
    assert "Substring Fallbacks: 1" in result.output
