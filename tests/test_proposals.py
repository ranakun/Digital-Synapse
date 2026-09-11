from __future__ import annotations

import shutil
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from synapse.index import connect, reindex
from synapse.proposals import apply_proposal, load_proposal, validate_proposal
from synapse.util import sha256_file

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    return v

def test_valid_proposal_passes(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    conn = connect(vault)
    
    # Write a valid proposal file
    prop_file = tmp_path / "prop.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base:
  - id: 01J00000000000000000000001
    content_hash: fakehash
  - id: me
    content_hash: fakehash
ops:
  - op: create_entity
    type: opportunity
    name: Test Opportunity
    tags: [test]
    properties:
      role: Engineer
      comp: 100000
    relations:
      - type: targets
        target: me
        direction: incoming
  - op: add_alias
    id: 01J00000000000000000000001
    alias: Alias Name
""", encoding="utf-8")
    
    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert errors == []

def test_invalid_ops_and_refs(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    conn = connect(vault)
    
    prop_file = tmp_path / "invalid_prop.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: invalid_confidence
base: []
ops:
  - op: unknown_op_type
  - op: create_entity
    type: invalid_entity_type
    name: Test
  - op: add_relation
    from: $new.5  # references index 5 at op index 2 (invalid)
    to: me
    type: invalid_rel_type
  - op: create_entity
    type: person
    name: Flat properties test
    properties:
      nested: { x: y }  # invalid non-flat property
""", encoding="utf-8")
    
    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert len(errors) > 0
    err_str = "\\n".join(errors)
    assert "Invalid proposal confidence" in err_str
    assert "unknown op type" in err_str
    assert "unknown entity type" in err_str
    assert "references invalid $new.5" in err_str
    assert "unknown relation type" in err_str
    assert "non-flat value" in err_str

def test_relation_specs_reject_from_to_keys(vault: Path, tmp_path: Path) -> None:
    """create_entity relation specs must use target — from/to keys were silently
    dropped by the indexer (the phase-10 recruits_for data bug)."""
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "from_key_prop.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base:
  - id: 01J00000000000000000000001
    content_hash: fakehash
ops:
  - op: create_entity
    type: opportunity
    name: Test Opportunity
    relations:
      - type: recruits_for
        from: 01J00000000000000000000001
        direction: incoming
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    err_str = "\n".join(errors)
    assert "not 'from'" in err_str
    assert "missing required 'target'" in err_str


def test_new_ref_must_point_at_create_entity(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "bad_new_ref.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base:
  - id: 01J00000000000000000000001
    content_hash: fakehash
  - id: me
    content_hash: fakehash
ops:
  - op: add_alias
    id: 01J00000000000000000000001
    alias: First Alias
  - op: add_relation
    from: me
    to: $new.0   # op 0 is add_alias, not create_entity
    type: knows
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert any("not a create_entity op" in e for e in errors)


def test_touching_me_requires_base_entry(vault: Path, tmp_path: Path) -> None:
    """The owner file gets the same stale-check protection as any entity."""
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "me_no_base.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base: []
ops:
  - op: add_relation
    from: me
    to: 01J00000000000000000000001
    type: knows
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert any("Entity me is touched" in e for e in errors)


def test_missing_base_entry(vault: Path, tmp_path: Path) -> None:
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "missing_base.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base: []
ops:
  - op: add_alias
    id: 01J00000000000000000000001  # touches existing ID but base is empty!
    alias: Alias Name
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert len(errors) > 0
    assert "missing from base entries" in "\\n".join(errors)


def test_archive_entity_validation_rejects_missing_id_and_reason(vault: Path, tmp_path: Path) -> None:
    """G4: archive_entity requires both a target id and a non-empty reason."""
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "archive_missing.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base: []
ops:
  - op: archive_entity
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    err_str = "\n".join(errors)
    assert "id must be a string" in err_str
    assert "archive_entity must specify a non-empty reason string" in err_str


def test_archive_entity_refuses_me(vault: Path, tmp_path: Path) -> None:
    """G4: 'me' can never be archived — refusing to orphan the owner."""
    reindex(vault)
    conn = connect(vault)
    me_hash = conn.execute("SELECT content_hash FROM entities WHERE id = 'me'").fetchone()["content_hash"]

    prop_file = tmp_path / "archive_me.yaml"
    prop_file.write_text(f"""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base:
  - id: me
    content_hash: {me_hash}
ops:
  - op: archive_entity
    id: me
    reason: this should never be allowed
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert any("refuses to archive 'me'" in e for e in errors)


def test_archive_entity_refuses_unknown_id(vault: Path, tmp_path: Path) -> None:
    """G4: an id that doesn't exist in the index is rejected, same as every op."""
    reindex(vault)
    conn = connect(vault)

    prop_file = tmp_path / "archive_unknown.yaml"
    prop_file.write_text("""
proposal: 01J00000000000000000000000
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Just testing
confidence: high
base:
  - id: 01J00000000000000000000999
    content_hash: fakehash
ops:
  - op: archive_entity
    id: 01J00000000000000000000999
    reason: this id was never real
""", encoding="utf-8")

    proposal = load_proposal(prop_file)
    errors = validate_proposal(conn, proposal)
    assert any("does not exist in the database" in e for e in errors)


def test_recommend_verify_notice_when_allow_verify_false(vault: Path, tmp_path: Path) -> None:
    """When a proposal has recommend_verify ops and allow_verify=False,
    a notice should be printed."""
    reindex(vault)

    # Get the content hashes of the entities we'll reference
    person_path = vault / "entities" / "people" / "example-person.md"
    me_path = vault / "entities" / "people" / "me.md"
    person_hash = sha256_file(person_path)
    me_hash = sha256_file(me_path)

    prop_file = tmp_path / "recommend_verify_prop.yaml"
    prop_file.write_text(f"""
proposal: 01J00000000000000000000002
agent: test-agent
created_at: 2026-06-11T12:00:00Z
rationale: Testing recommend_verify notice
confidence: high
base:
  - id: 01J00000000000000000000001
    content_hash: {person_hash}
  - id: me
    content_hash: {me_hash}
ops:
  - op: recommend_verify
    id: 01J00000000000000000000001
    reason: Verify this entity
  - op: recommend_verify
    id: me
    reason: Verify owner
""", encoding="utf-8")

    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        result = apply_proposal(vault, prop_file, execute=False, allow_verify=False)

    output = mock_stdout.getvalue()
    assert result["success"] is True
    assert "2 recommend_verify advisory(ies) skipped" in output
    assert "--allow-verify" in output
