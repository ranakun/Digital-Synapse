from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from synapse.cli import app
from synapse.index import connect, reindex
from synapse.util import read_frontmatter

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    return v

def test_apply_dry_run_and_execute(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    
    prop_path = vault / "proposals" / "pending" / "create_comp.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Create new company and add a relation
confidence: high
base:
  - id: me
    content_hash: 2c19958cf5f49ef47335d1e2e796030999551c6e127909cd349bd6161b9bf560
ops:
  - op: create_entity
    type: company
    name: Spec Company
    tags: [test]
    properties:
      website: https://spec.company
  - op: add_relation
    from: me
    to: $new.0
    type: works_at
""", encoding="utf-8")

    conn = connect(vault)
    me_row = conn.execute("SELECT content_hash FROM entities WHERE id = 'me'").fetchone()
    me_hash = me_row["content_hash"]
    conn.close()
    
    prop_path.write_text(prop_path.read_text().replace("2c19958cf5f49ef47335d1e2e796030999551c6e127909cd349bd6161b9bf560", me_hash))
    
    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--vault", str(vault)])
    assert result.exit_code == 0
    assert "create_entity" in result.output
    assert "Spec Company" in result.output
    
    comp_file = vault / "entities" / "companies" / "spec-company.md"
    assert not comp_file.exists()
    
    result_execute = runner.invoke(app, ["apply", str(prop_path), "--execute", "--vault", str(vault)])
    assert result_execute.exit_code == 0
    assert comp_file.exists()
    
    meta, body = read_frontmatter(comp_file)
    assert meta["review_status"] == "proposed"
    assert meta["properties"]["website"] == "https://spec.company"
    assert meta["provenance"]["extracted_by"] == "proposal:01J00000000000000000000000"
    
    comp_id = meta["id"]
    me_meta, _ = read_frontmatter(vault / "entities" / "people" / "me.md")
    rels = me_meta.get("relations") or []
    assert any(r.get("type") == "works_at" and r.get("target") == comp_id for r in rels)

    archived_prop = vault / "proposals" / "applied" / "create_comp.yaml"
    assert archived_prop.exists()
    with archived_prop.open(encoding="utf-8") as f:
        archived_data = yaml.safe_load(f)
    assert "result" in archived_data
    assert archived_data["result"]["ops"] == ["applied", "applied"]
    
    # Update archived prop's base hash to match current DB so it doesn't fail stale check
    conn = connect(vault)
    me_row = conn.execute("SELECT content_hash FROM entities WHERE id = 'me'").fetchone()
    new_me_hash = me_row["content_hash"]
    conn.close()
    
    archived_text = archived_prop.read_text(encoding="utf-8")
    archived_prop.write_text(archived_text.replace(me_hash, new_me_hash), encoding="utf-8")
    
    result_reapply = runner.invoke(app, ["apply", str(archived_prop), "--vault", str(vault)])
    assert result_reapply.exit_code == 0
    assert "noop" in result_reapply.output

def test_add_relation_preserves_edge_metadata(vault: Path) -> None:
    """add_relation must carry op.properties onto the edge (source -> sibling
    key, everything else under properties), matching create_entity. Regression
    for the branch that built rel_spec as only {type, target}."""
    reindex(vault)

    conn = connect(vault)
    me_hash = conn.execute("SELECT content_hash FROM entities WHERE id = 'me'").fetchone()["content_hash"]
    conn.close()

    prop_path = vault / "proposals" / "pending" / "rel_meta.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text(f"""
proposal: 01J00000000000000000000001
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Add a relation carrying edge metadata
confidence: high
base:
  - id: me
    content_hash: {me_hash}
ops:
  - op: create_entity
    type: company
    name: Meta Edge Co
    tags: [test]
  - op: add_relation
    from: me
    to: $new.0
    type: former_employee_of
    properties:
      source: owner-recall
      started_on: 2023-01
      current: false
""", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--execute", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    me_meta, _ = read_frontmatter(vault / "entities" / "people" / "me.md")
    rel = next(
        r for r in (me_meta.get("relations") or [])
        if r.get("type") == "former_employee_of"
    )
    # `source` lifted to sibling (indexer reads it as source_file)
    assert rel.get("source") == "owner-recall"
    # remaining metadata preserved under properties
    assert rel.get("properties", {}).get("started_on") == "2023-01"
    assert rel.get("properties", {}).get("current") is False
    # source must NOT be duplicated inside properties
    assert "source" not in rel.get("properties", {})


def test_remove_relation_dry_run_and_exact_source_match(vault: Path) -> None:
    me_path = vault / "entities" / "people" / "me.md"
    metadata, body = read_frontmatter(me_path)
    metadata["relations"].append(
        {
            "type": "has_goal",
            "target": "01J00000000000000000000005",
            "properties": {},
            "source": "other-source",
        }
    )
    metadata["relations"].append(
        {
            "type": "mentioned_in",
            "target": "01J00000000000000000000005",
            "direction": "outgoing",
            "properties": {},
            "source": "legacy-importer",
        }
    )
    from synapse.util import write_frontmatter

    write_frontmatter(me_path, metadata, body)
    reindex(vault, full=True)
    conn = connect(vault)
    me_hash = conn.execute(
        "SELECT content_hash FROM entities WHERE id = 'me'"
    ).fetchone()["content_hash"]
    goal_hash = conn.execute(
        "SELECT content_hash FROM entities WHERE id = '01J00000000000000000000005'"
    ).fetchone()["content_hash"]
    conn.close()

    prop_path = vault / "proposals" / "pending" / "remove_relation.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text(f"""
proposal: 01J00000000000000000000002
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Remove one stale source-specific edge
confidence: high
base:
  - id: me
    content_hash: {me_hash}
  - id: 01J00000000000000000000005
    content_hash: {goal_hash}
ops:
  - op: remove_relation
    from: me
    to: 01J00000000000000000000005
    type: has_goal
    properties:
      source: test-fixture
  - op: remove_relation
    from: me
    to: 01J00000000000000000000005
    type: mentioned_in
    direction: outgoing
""", encoding="utf-8")

    before = me_path.read_bytes()
    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "remove_relation" in result.output
    assert me_path.read_bytes() == before

    result = runner.invoke(
        app, ["apply", str(prop_path), "--execute", "--vault", str(vault)]
    )
    assert result.exit_code == 0, result.output
    metadata, _ = read_frontmatter(me_path)
    sources = {
        relation.get("source")
        for relation in metadata["relations"]
        if relation.get("type") == "has_goal"
        and relation.get("target") == "01J00000000000000000000005"
    }
    assert sources == {"other-source"}
    assert not any(
        relation.get("type") == "mentioned_in"
        and relation.get("target") == "01J00000000000000000000005"
        for relation in metadata["relations"]
    )


def _archive_prop_yaml(person_hash: str, *, reason: str = "duplicate junk entity from a bad parse") -> str:
    return f"""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Remove a junk entity through the reviewed proposal path
confidence: high
base:
  - id: 01J00000000000000000000001
    content_hash: {person_hash}
ops:
  - op: archive_entity
    id: 01J00000000000000000000001
    reason: {reason}
"""


def test_archive_entity_dry_run_previews_without_touching_vault(vault: Path) -> None:
    """G4: dry-run previews the file move + dangling edges but changes nothing."""
    reindex(vault)
    conn = connect(vault)
    person_hash = conn.execute(
        "SELECT content_hash FROM entities WHERE id = '01J00000000000000000000001'"
    ).fetchone()["content_hash"]
    conn.close()

    prop_path = vault / "proposals" / "pending" / "archive_dry.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text(_archive_prop_yaml(person_hash), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "archive_entity" in result.output
    assert "entities/archive" in result.output.replace("\\", "/")
    # The dangling `attended` edge from example-event.md is called out in the preview.
    assert "edge(s) will dangle" in result.output

    person_path = vault / "entities" / "people" / "example-person.md"
    assert person_path.exists()
    archive_dir = vault / "entities" / "archive"
    assert not archive_dir.exists() or not any(archive_dir.glob("*.md"))
    # Proposal stays pending — dry-run never archives it.
    assert prop_path.exists()


def test_archive_entity_execute_moves_file_reindex_absent_and_check_surfaces_dangling(
    vault: Path,
) -> None:
    """G4: --execute moves the file to entities/archive/, reindex shows the
    entity gone, and the dangling edge left on example-event.md surfaces via
    `synapse check` (the SAFER default: leave + surface, don't silently prune)."""
    reindex(vault)
    conn = connect(vault)
    person_hash = conn.execute(
        "SELECT content_hash FROM entities WHERE id = '01J00000000000000000000001'"
    ).fetchone()["content_hash"]
    conn.close()

    prop_path = vault / "proposals" / "pending" / "archive_exec.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text(_archive_prop_yaml(person_hash), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--execute", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    person_path = vault / "entities" / "people" / "example-person.md"
    assert not person_path.exists()

    archive_dir = vault / "entities" / "archive"
    archived_files = list(archive_dir.glob("*.md"))
    assert len(archived_files) == 1
    meta, _ = read_frontmatter(archived_files[0])
    assert meta.get("archived") is True
    assert meta.get("archived_reason") == "duplicate junk entity from a bad parse"
    assert meta.get("review_status") == "proposed"
    assert meta.get("id") == "01J00000000000000000000001"

    # Reindex: the archived entity is fully absent from the live index.
    reindex(vault, full=True)
    conn = connect(vault)
    try:
        ids = {row["id"] for row in conn.execute("SELECT id FROM entities").fetchall()}
    finally:
        conn.close()
    assert "01J00000000000000000000001" not in ids

    # check surfaces the dangling `attended` edge left on example-event.md
    # instead of it being silently pruned (the documented dangling-edge policy).
    from synapse.maintenance import check_vault

    report = check_vault(vault_path=vault)
    issue_messages = " ".join(issue["message"] for issue in report["issues"])
    assert "01J00000000000000000000001" in issue_messages


def test_archive_entity_stale_refusal(vault: Path) -> None:
    """G4: a drifted base hash refuses the archive, same as every other op."""
    reindex(vault)

    prop_path = vault / "proposals" / "pending" / "archive_stale.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text(_archive_prop_yaml("mismatched_hash_here"), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--execute", "--vault", str(vault)])
    assert result.exit_code == 1
    assert "drifted: 01J00000000000000000000001" in result.output

    # Nothing was touched.
    person_path = vault / "entities" / "people" / "example-person.md"
    assert person_path.exists()


def test_apply_stale_refusal(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    
    prop_path = vault / "proposals" / "pending" / "stale.yaml"
    prop_path.parent.mkdir(parents=True, exist_ok=True)
    prop_path.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Update property
confidence: high
base:
  - id: me
    content_hash: mismatched_hash_here
ops:
  - op: update_properties
    id: me
    set:
      status: busy
""", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["apply", str(prop_path), "--execute", "--vault", str(vault)])
    assert result.exit_code == 1
    assert "drifted: me" in result.output
