from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from synapse.config import init_vault
from synapse.index import all_relations, connect, reindex

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
VAULT_ROOT = FIXTURE_ROOT / "vault"


def test_fixture_vault_matches_sds_layout() -> None:
    expected_paths = [
        VAULT_ROOT / ".synapse" / "config.yaml",
        VAULT_ROOT / ".gitignore",
        VAULT_ROOT / "entities" / "people" / "example-person.md",
        VAULT_ROOT / "entities" / "companies" / "example-company.md",
        VAULT_ROOT / "entities" / "projects" / "example-project-alpha.md",
        VAULT_ROOT / "entities" / "projects" / "example-project-beta.md",
        VAULT_ROOT / "entities" / "goals" / "example-goal.md",
        VAULT_ROOT / "entities" / "finance" / "example-account.md",
        VAULT_ROOT / "inbox",
        VAULT_ROOT / "inbox" / "processed",
        VAULT_ROOT / "ledgers" / "example-account.csv",
    ]

    for path in expected_paths:
        assert path.exists(), path

    config = yaml.safe_load((VAULT_ROOT / ".synapse" / "config.yaml").read_text(encoding="utf-8"))
    assert config["vault_path"] == "."
    assert config["gate"]["auto_reindex_on_commit"] is True
    assert config["llm"]["zdr_required"] is True
    assert config["llm"]["zdr_acknowledged"] is False
    assert config["index"]["db_path"] == ".synapse/index.db"
    assert ".synapse/index.db" in (VAULT_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_fixture_source_is_fictional_and_safe() -> None:
    source = (FIXTURE_ROOT / "sources" / "sample-ingest.md").read_text(encoding="utf-8")
    assert "Example Person" in source
    assert "Example Company" in source
    assert "This file is entirely fictional" in source
    assert "@" not in source


def test_init_installs_secret_blocking_pre_commit_hook(tmp_path: Path) -> None:
    vault = init_vault(tmp_path / "vault")
    hook = vault / ".git" / "hooks" / "pre-commit"

    assert hook.exists()
    assert "Digital Synapse secret scan" in hook.read_text(encoding="utf-8")

    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "Digital Synapse Test"], cwd=vault, check=True)

    leaked = vault / "leak.txt"
    leaked.write_text("SYNAPSE_LLM_API_KEY=sk-test-secret-value\n", encoding="utf-8")
    subprocess.run(["git", "add", "leak.txt"], cwd=vault, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "should be blocked"],
        cwd=vault,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "secret scan blocked" in (result.stdout + result.stderr).lower()


def test_deleting_index_and_rebuilding_preserves_indexed_facts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(VAULT_ROOT, vault)

    reindex(vault, full=True)
    conn = connect(vault)
    try:
        before_entities = [
            dict(row)
            for row in conn.execute(
                "SELECT id, type, name, file_path, frontmatter, body, review_status FROM entities ORDER BY id"
            ).fetchall()
        ]
        before_relations = all_relations(conn, include_weak=True)
    finally:
        conn.close()

    db_path = vault / ".synapse" / "index.db"
    for path in [db_path, db_path.with_name("index.db-shm"), db_path.with_name("index.db-wal")]:
        if path.exists():
            path.unlink()

    reindex(vault, full=True)
    conn = connect(vault)
    try:
        after_entities = [
            dict(row)
            for row in conn.execute(
                "SELECT id, type, name, file_path, frontmatter, body, review_status FROM entities ORDER BY id"
            ).fetchall()
        ]
        after_relations = all_relations(conn, include_weak=True)
    finally:
        conn.close()

    assert json.dumps(after_entities, sort_keys=True) == json.dumps(before_entities, sort_keys=True)
    assert json.dumps(after_relations, sort_keys=True) == json.dumps(before_relations, sort_keys=True)
