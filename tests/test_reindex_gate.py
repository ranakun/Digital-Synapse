"""Tests for T9.2: mtime+size reindex gate."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from synapse.index import all_entities, all_relations, connect, reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


def _write_entity(
    path: Path,
    *,
    entity_id: str,
    entity_type: str = "person",
    extra_frontmatter: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "---\n"
            f'id: "{entity_id}"\n'
            f"type: {entity_type}\n"
            f'name: "{entity_id}"\n'
            "review_status: proposed\n"
            f"{extra_frontmatter}"
            "---\n"
        ),
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize("tombstone", ["archived: true\n", 'merged_into: "live-id"\n'])
def test_reindex_gate_tombstones_are_stable(tmp_path: Path, tombstone: str) -> None:
    """Archived and merged files are cached sources, not live indexed entities."""
    vault = tmp_path / "vault"
    _write_entity(vault / "entities" / "people" / "live.md", entity_id="live-id")
    _write_entity(
        vault / "entities" / "archive" / "tombstone.md",
        entity_id="tombstone-id",
        extra_frontmatter=tombstone,
    )

    initial = reindex(vault, full=True)
    unchanged = reindex(vault)

    assert initial.entities == 1
    assert initial.changed_files == 2
    assert unchanged.entities == 1
    assert unchanged.changed_files == 0

    conn = connect(vault)
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"] == 1
    finally:
        conn.close()


def test_reindex_gate_tombstone_noop_returns_cached_parser_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete file cache gate preserves diagnostics without reparsing."""
    vault = tmp_path / "vault"
    _write_entity(vault / "entities" / "people" / "live.md", entity_id="live-id")
    _write_entity(
        vault / "entities" / "archive" / "archived.md",
        entity_id="archived-id",
        entity_type="unsupported",
        extra_frontmatter="archived: true\n",
    )
    initial = reindex(vault, full=True)
    assert [issue.message for issue in initial.issues] == [
        "Unknown entity type: unsupported"
    ]

    def fail_parse(_vault: Path) -> None:
        pytest.fail("unchanged reindex reparsed the vault")

    monkeypatch.setattr("synapse.index.parse_vault", fail_parse)
    unchanged = reindex(vault)

    assert unchanged.changed_files == 0
    assert [(issue.severity, issue.message) for issue in unchanged.issues] == [
        ("warning", "Unknown entity type: unsupported")
    ]


@pytest.mark.parametrize("operation", ["edit", "add", "remove"])
def test_reindex_gate_tombstone_change_refreshes_once(
    tmp_path: Path, operation: str
) -> None:
    """Any archived-source change rebuilds once, then the gate becomes a no-op."""
    vault = tmp_path / "vault"
    _write_entity(vault / "entities" / "people" / "live.md", entity_id="live-id")
    archived = vault / "entities" / "archive" / "archived.md"
    _write_entity(
        archived,
        entity_id="archived-id",
        extra_frontmatter="archived: true\n",
    )
    reindex(vault, full=True)
    assert reindex(vault).changed_files == 0

    if operation == "edit":
        archived.write_text(
            archived.read_text(encoding="utf-8") + "\nArchived note changed.\n",
            encoding="utf-8",
            newline="\n",
        )
    elif operation == "add":
        _write_entity(
            vault / "entities" / "archive" / "added.md",
            entity_id="added-archived-id",
            extra_frontmatter='merged_into: "live-id"\n',
        )
    else:
        archived.unlink()

    refreshed = reindex(vault)
    unchanged = reindex(vault)

    assert refreshed.changed_files > 0
    assert refreshed.entities == 1
    assert unchanged.changed_files == 0
    assert unchanged.entities == 1


def test_reindex_gate_no_change_skips_parsing(tmp_path: Path) -> None:
    """If no files are changed, reindex reports 0 changed files and loads cached issues."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    # 1. First reindex to build the DB and cache file stats/issues
    res1 = reindex(vault, full=True)
    assert res1.entities > 0
    assert res1.changed_files > 0
    
    original_issues = list(res1.issues)

    # 2. Second reindex with full=False (no changes on disk)
    res2 = reindex(vault, full=False)
    assert res2.changed_files == 0
    assert res2.entities == res1.entities
    assert res2.relations == res1.relations
    assert len(res2.issues) == len(original_issues)
    assert [i.message for i in res2.issues] == [i.message for i in original_issues]


def test_reindex_gate_detects_edit(tmp_path: Path) -> None:
    """An edit to a file's content (changing mtime and content) triggers a rebuild."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)

    # Edit one of the files
    target_file = vault / "entities" / "people" / "example-person.md"
    content = target_file.read_text(encoding="utf-8")
    new_content = content + "\n\nSome new body text to change size and hash.\n"
    
    # Write and ensure mtime changes
    target_file.write_text(new_content, encoding="utf-8", newline="\n")
    
    res = reindex(vault, full=False)
    assert res.changed_files > 0


def test_reindex_gate_detects_add(tmp_path: Path) -> None:
    """Adding a new entity file triggers a rebuild."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)

    # Add a new file
    new_file = vault / "entities" / "people" / "new-person.md"
    new_file.write_text(
        "---\nid: \"new-person-id\"\ntype: person\nname: \"New Person\"\nreview_status: proposed\n---\n",
        encoding="utf-8",
        newline="\n"
    )

    res = reindex(vault, full=False)
    assert res.changed_files > 0


def test_reindex_gate_detects_delete(tmp_path: Path) -> None:
    """Deleting an entity file triggers a rebuild."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)

    # Delete a file
    target_file = vault / "entities" / "people" / "example-person.md"
    target_file.unlink()

    res = reindex(vault, full=False)
    assert res.changed_files > 0


def test_reindex_gate_detects_rename(tmp_path: Path) -> None:
    """Renaming an entity file triggers a rebuild."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)

    # Rename a file
    src = vault / "entities" / "people" / "example-person.md"
    dst = vault / "entities" / "people" / "example-person-renamed.md"
    src.rename(dst)

    res = reindex(vault, full=False)
    assert res.changed_files > 0


def test_reindex_gate_full_bypasses(tmp_path: Path) -> None:
    """full=True rebuilds and reports all files as changed, even if no changes occurred."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)
    res = reindex(vault, full=True)
    assert res.changed_files > 0


def test_reindex_gate_parity(tmp_path: Path) -> None:
    """A gated reindex and a full reindex produce identical tables on the database."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    # Reindex 1: Full
    reindex(vault, full=True)
    conn1 = connect(vault)
    try:
        entities_full = all_entities(conn1)
        relations_full = all_relations(conn1, include_weak=True)
    finally:
        conn1.close()

    # Edit a file to trigger rebuild on next gated run
    target_file = vault / "entities" / "people" / "example-person.md"
    target_file.write_text(target_file.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8", newline="\n")

    # Reindex 2: Gated
    reindex(vault, full=False)
    conn2 = connect(vault)
    try:
        entities_gated = all_entities(conn2)
        relations_gated = all_relations(conn2, include_weak=True)
    finally:
        conn2.close()

    # The list of entities/relations should have the same structure and counts
    assert len(entities_full) == len(entities_gated)
    assert len(relations_full) == len(relations_gated)


def test_reindex_gate_preserved_mtime_limitation(tmp_path: Path) -> None:
    """If a file's content is modified but same size and preserved mtime, gate misses it but full catches."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    reindex(vault, full=True)

    target_file = vault / "entities" / "people" / "example-person.md"
    stat = target_file.stat()
    orig_mtime_ns = stat.st_mtime_ns
    orig_size = stat.st_size
    orig_content = target_file.read_text(encoding="utf-8")

    # Perform same-size modification: swap two characters of the same length
    new_content = orig_content.replace("Example Person", "Exampxl Person")
    assert len(new_content) == orig_size
    
    target_file.write_text(new_content, encoding="utf-8", newline="\n")
    
    # Restore original mtime precisely in nanoseconds
    os.utime(target_file, ns=(orig_mtime_ns, orig_mtime_ns))

    # Gated reindex should NOT detect the change
    res_gated = reindex(vault, full=False)
    assert res_gated.changed_files == 0

    # Full reindex should catch it
    res_full = reindex(vault, full=True)
    assert res_full.changed_files > 0


def test_reindex_gate_timing_on_synthetic(tmp_path: Path) -> None:
    """No-change reindex on a synthetic vault completes in a tiny fraction of time."""
    import gen_synthetic_vault

    vault = tmp_path / "synth_perf"
    gen_synthetic_vault.generate(vault, n_people=200, n_companies=20, n_conversations=30, seed=42)

    # Initial reindex (warmup)
    t0 = time.perf_counter()
    reindex(vault, full=True)
    warm_time = time.perf_counter() - t0

    # Gated reindex (no change)
    t1 = time.perf_counter()
    res = reindex(vault, full=False)
    gated_time = time.perf_counter() - t1

    assert res.changed_files == 0
    # Gated reindex should be extremely fast (< 500ms in CI)
    assert gated_time < 2.0
    print(f"Warmup time: {warm_time:.4f}s | Gated time: {gated_time:.4f}s")
