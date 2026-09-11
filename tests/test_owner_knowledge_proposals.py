"""The owner-knowledge profile is a whole-proposal invariant, not a write side effect."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from synapse.index import connect, reindex
from synapse.maintenance import check_vault
from synapse.proposals import apply_proposal, load_proposal, validate_proposal
from synapse.util import read_frontmatter, write_frontmatter

SOURCE = "01J00000000000000000000011"
OTHER_SOURCE = "01J00000000000000000000012"
FINDING = "01J00000000000000000000021"
OTHER_FINDING = "01J00000000000000000000022"
PROPOSAL = "01J00000000000000000000031"
NAVIGATION = "01J00000000000000000000041"

BODY = """## Statement
The owner reports learning best with concrete examples.

## Conditions and limits
This is a contextual owner report, not a measured learning rate.

## Evidence
The owner described an example in the recorded conversation.
"""


def _properties(claim_key="learning-examples", **changes):
    properties = {
        "knowledge_profile": "owner-knowledge-v1",
        "subject_id": "me",
        "record_kind": "finding",
        "claim_key": claim_key,
        "facets": ["learning"],
        "epistemic_basis": ["owner-report"],
        "owner_position": "stated",
        "lifecycle": "current",
        "as_of": "2026-09-01",
    }
    properties.update(changes)
    return properties


def _edge(target=SOURCE, roles=None, **properties):
    return {
        "type": "related_to",
        "target": target,
        "source": "inbox/source.md",
        "properties": {
            "roles": roles or ["evidence"],
            "note": "The recorded account supports this finding.",
            "scope": "The focal statement and its limits.",
            **properties,
        },
    }


def _write(vault, entity_id, name, *, entity_type="insight", properties=None, relations=None, body=BODY):
    folder = {"insight": "insights", "conversation": "conversations", "person": "people"}[entity_type]
    path = vault / "entities" / folder / f"{name}.md"
    relations = deepcopy(relations or [])
    if properties and "knowledge_profile" in properties and properties.get("record_kind") != "navigation":
        relations.append(_edge(NAVIGATION, ["member"]))
    write_frontmatter(path, {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "review_status": "proposed",
        "properties": properties or {},
        "relations": relations or [],
    }, body)
    return path


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    _write(root, "me", "me", entity_type="person", body="# Synthetic owner")
    for entity_id, name in [(SOURCE, "source"), (OTHER_SOURCE, "other-source")]:
        _write(root, entity_id, name, entity_type="conversation", properties={
            "source_family": "synthetic-session",
            "source_file": "inbox/source.md",
        }, body="The synthetic owner described preferring concrete examples.")
    _write(root, NAVIGATION, "knowledge-navigation", properties=_properties("navigation", record_kind="navigation"))
    (root / "inbox").mkdir()
    (root / "inbox/source.md").write_text("Synthetic source.\n")
    reindex(root)
    return root


def _finding(vault, entity_id=FINDING, name="finding", **changes):
    return _write(vault, entity_id, name, properties=_properties(**changes), relations=[_edge()])


def _create(name="new-finding", **changes):
    return {
        "op": "create_entity", "type": "insight", "name": name,
        "properties": _properties(**changes), "relations": [_edge(), _edge(NAVIGATION, ["member"])], "body": BODY,
    }


def _proposal(vault, ops, *, proposal_id=PROPOSAL):
    reindex(vault)
    conn = connect(vault)
    try:
        base = [dict(row) for row in conn.execute("SELECT id, content_hash FROM entities")]
    finally:
        conn.close()
    path = vault / "proposals/pending/knowledge.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "proposal": proposal_id, "agent": "synthetic-test", "created_at": "2026-09-01",
        "rationale": "Exercise reviewed knowledge capture.", "confidence": "high",
        "base": base, "ops": ops,
    }, sort_keys=False))
    return path


def _snapshot(vault):
    return {str(path.relative_to(vault)): path.read_bytes() for path in (vault / "entities").rglob("*.md")}


def _apply(vault, path, **kwargs):
    with patch("synapse.maintenance.nearest_duplicates", return_value=[]):
        return apply_proposal(vault, path, **kwargs)


def _assert_rejected_without_writes(vault, path, fragment):
    before = _snapshot(vault)
    result = _apply(vault, path, execute=True)
    assert not result["success"], result
    assert fragment.casefold() in " ".join(result["errors"]).casefold(), result
    assert _snapshot(vault) == before
    assert path.exists()
    return result


def test_invalid_new_record_rejects_entire_proposal_before_any_write(vault):
    invalid = _create()
    invalid["properties"].pop("epistemic_basis")
    path = _proposal(vault, [
        {"op": "create_entity", "type": "project", "name": "Unrelated project"},
        invalid,
    ])
    _assert_rejected_without_writes(vault, path, "epistemic_basis")


@pytest.mark.parametrize("field", ["owner_position", "claim_key", "knowledge_profile"])
def test_update_cannot_remove_required_profile_properties(vault, field):
    _finding(vault)
    path = _proposal(vault, [{"op": "update_properties", "id": FINDING, "unset": [field]}])
    _assert_rejected_without_writes(vault, path, "profile" if field == "knowledge_profile" else field)


def test_promoting_existing_record_checks_its_actual_body(vault):
    _write(vault, FINDING, "legacy", body="A headline without qualifications.")
    path = _proposal(vault, [{"op": "update_properties", "id": FINDING, "set": _properties()}])
    _assert_rejected_without_writes(vault, path, "Conditions and limits")


def test_valid_property_amendment_preserves_existing_body_and_proposed_status(vault):
    record = _finding(vault)
    before_body = read_frontmatter(record)[1]
    path = _proposal(vault, [{"op": "update_properties", "id": FINDING, "set": {"owner_position": "accepted"}}])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    metadata, body = read_frontmatter(record)
    assert metadata["properties"]["owner_position"] == "accepted"
    assert metadata["review_status"] == "proposed"
    assert body == before_body


def test_revision_applies_and_reapply_uses_existing_provenance_identity(vault):
    _finding(vault)
    correction = _create("refined-finding")
    correction["relations"].append(_edge(FINDING, ["revises"], scope="The complete focal statement."))
    ops = [
        {"op": "update_properties", "id": FINDING, "set": {"lifecycle": "historical"}},
        correction,
    ]
    path = _proposal(vault, ops)
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert result["results"] == ["applied", "applied"]
    before = _snapshot(vault)
    # Refresh bases exactly as for a reviewed replay; do not weaken stale checks.
    path = _proposal(vault, ops)
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert result["results"] == ["noop", "noop"]
    assert _snapshot(vault) == before
    report = check_vault(vault)
    assert report["issues"] == []


def test_two_unrelated_current_versions_are_rejected(vault):
    _finding(vault)
    path = _proposal(vault, [_create("unrelated-current")])
    _assert_rejected_without_writes(vault, path, "current")


def test_revision_cycle_is_rejected_before_writes(vault):
    _finding(vault)
    _finding(vault, OTHER_FINDING, "historical-finding", lifecycle="historical")
    path = _proposal(vault, [
        {"op": "add_relation", "from": FINDING, "to": OTHER_FINDING, "type": "related_to",
         "properties": {"roles": ["revises"], "scope": "The complete focal statement.", "note": "Revision."}},
        {"op": "add_relation", "from": OTHER_FINDING, "to": FINDING, "type": "related_to",
         "properties": {"roles": ["revises"], "scope": "The complete focal statement.", "note": "Reverse revision."}},
    ])
    _assert_rejected_without_writes(vault, path, "cycle")


def test_malformed_new_roles_are_rejected(vault):
    _finding(vault)
    path = _proposal(vault, [{
        "op": "add_relation", "from": FINDING, "to": OTHER_SOURCE, "type": "related_to",
        "properties": {"roles": ["unrecognized-role"], "note": "Invalid relation."},
    }])
    _assert_rejected_without_writes(vault, path, "role")


def test_existing_relation_noop_does_not_replace_valid_metadata(vault):
    _finding(vault)
    path = _proposal(vault, [{
        "op": "add_relation", "from": FINDING, "to": SOURCE, "type": "related_to",
        "properties": {"roles": ["invalid-but-never-written"]},
    }])
    before = _snapshot(vault)
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert result["results"] == ["noop"]
    assert _snapshot(vault) == before


def test_incoming_evidence_ownership_matches_applied_graph(vault):
    finding_path = _finding(vault)
    path = _proposal(vault, [
        {"op": "remove_relation", "from": FINDING, "to": SOURCE, "type": "related_to"},
        {"op": "add_relation", "from": FINDING, "to": SOURCE, "type": "related_to", "direction": "incoming",
         "properties": {"roles": ["evidence"], "scope": "The focal statement.", "note": "Recorded support."}},
    ])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert [r["properties"]["roles"] for r in read_frontmatter(finding_path)[0]["relations"]] == [["member"]]
    relation = read_frontmatter(vault / "entities/conversations/source.md")[0]["relations"][0]
    assert relation["target"] == FINDING
    assert relation["direction"] == "incoming"
    assert relation["properties"]["roles"] == ["evidence"]
    assert check_vault(vault)["issues"] == []


def test_archiving_linked_evidence_cannot_bypass_graph_validation(vault):
    _finding(vault)
    path = _proposal(vault, [{"op": "archive_entity", "id": SOURCE, "reason": "Synthetic archival request."}])
    _assert_rejected_without_writes(vault, path, SOURCE)


def test_explicit_reference_cleanup_allows_evidence_archive(vault):
    _write(vault, FINDING, "finding", properties=_properties(), relations=[_edge(), _edge(OTHER_SOURCE)])
    path = _proposal(vault, [
        {"op": "remove_relation", "from": FINDING, "to": SOURCE, "type": "related_to",
         "properties": {"source": "inbox/source.md", "roles": ["evidence"]}},
        {"op": "archive_entity", "id": SOURCE, "reason": "The other source covers this account."},
    ])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert not (vault / "entities/conversations/source.md").exists()
    assert check_vault(vault)["issues"] == []


def test_nonmatching_remove_does_not_hide_dangling_reference(vault):
    _finding(vault)
    path = _proposal(vault, [
        {"op": "remove_relation", "from": FINDING, "to": SOURCE, "type": "related_to",
         "properties": {"source": "a-different-source"}},
        {"op": "archive_entity", "id": SOURCE, "reason": "Synthetic archive request."},
    ])
    _assert_rejected_without_writes(vault, path, SOURCE)


def test_merge_retargets_evidence_references_in_projection_and_files(vault):
    finding_path = _finding(vault)
    path = _proposal(vault, [{"op": "merge", "keep": OTHER_SOURCE, "merge": SOURCE}])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert read_frontmatter(finding_path)[0]["relations"][0]["target"] == OTHER_SOURCE
    assert not (vault / "entities/conversations/source.md").exists()
    assert check_vault(vault)["issues"] == []


def test_merge_cannot_turn_focal_insight_into_unsupported_entity_type(vault):
    _finding(vault)
    path = _proposal(vault, [{"op": "merge", "keep": SOURCE, "merge": FINDING}])
    _assert_rejected_without_writes(vault, path, "type")


def test_check_reports_profile_issues_with_path_and_consistent_count(vault):
    properties = _properties()
    properties.pop("owner_position")
    record = _write(vault, FINDING, "malformed", properties=properties, relations=[_edge()])
    report = check_vault(vault)
    issues = [item for item in report["issues"] if "owner_position" in item["message"]]
    assert len(issues) == 1
    assert Path(issues[0]["file_path"]).name == record.name
    assert issues[0]["severity"] == "error"
    assert report["summary"]["issue_count"] == len(report["issues"])


def test_existing_profile_errors_do_not_block_unrelated_work_or_partial_repair(vault):
    properties = _properties()
    properties.pop("owner_position")
    properties.pop("epistemic_basis")
    _write(vault, FINDING, "malformed", properties=properties, relations=[_edge()])
    path = _proposal(vault, [
        {"op": "create_entity", "type": "project", "name": "Independent project", "body": "Ordinary legacy format."},
        {"op": "update_properties", "id": FINDING, "set": {"owner_position": "stated"}},
    ])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    issues = check_vault(vault)["issues"]
    assert any("epistemic_basis" in item["message"] for item in issues)
    assert not any("owner_position" in item["message"] for item in issues)


def test_pure_validation_does_not_mutate_or_reindex(vault):
    _finding(vault)
    path = _proposal(vault, [{"op": "update_properties", "id": FINDING, "set": {"owner_position": "accepted"}}])
    before = _snapshot(vault)
    conn = connect(vault)
    try:
        prior_rows = deepcopy([tuple(row) for row in conn.execute("SELECT * FROM entities ORDER BY id")])
        with patch("synapse.proposals.reindex", side_effect=AssertionError("Not in pure validation")):
            assert validate_proposal(conn, load_proposal(path)) == []
        assert prior_rows == [tuple(row) for row in conn.execute("SELECT * FROM entities ORDER BY id")]
    finally:
        conn.close()
    assert _snapshot(vault) == before


def test_apply_refreshes_canonical_body_before_stale_check(vault):
    record = _finding(vault)
    path = _proposal(vault, [{"op": "update_properties", "id": FINDING, "set": {"owner_position": "accepted"}}])
    metadata, body = read_frontmatter(record)
    write_frontmatter(record, metadata, body + "\nA later owner clarification changes the source.\n")
    result = _assert_rejected_without_writes(vault, path, "drifted")
    assert result["stale"] is True


@pytest.mark.parametrize("properties", [
    {"roles": "evidence", "note": "A string is not a role list."},
    {"roles": ["qualifies"], "note": "Scope is intentionally absent."},
    {"roles": ["unrecognized-role"], "note": "Invalid incoming role."},
])
def test_incoming_roles_declared_on_legacy_record_are_validated(vault, properties):
    _finding(vault)
    path = _proposal(vault, [{
        "op": "add_relation", "from": FINDING, "to": OTHER_SOURCE,
        "type": "related_to", "direction": "incoming", "properties": properties,
    }])
    _assert_rejected_without_writes(vault, path, "scope" if properties["roles"] == ["qualifies"] else "role")


def test_new_collection_references_resolve_before_graph_validation(vault):
    navigation = _create("new-navigation", claim_key="new.navigation", record_kind="navigation")
    navigation["relations"] = []
    finding = _create("member-of-new-navigation")
    finding["relations"] = [_edge(), _edge("$new.0", ["member"])]
    path = _proposal(vault, [navigation, finding])
    result = _apply(vault, path, execute=True)
    assert result["success"], result
    assert check_vault(vault)["issues"] == []
    fm, _ = read_frontmatter(vault / "entities/insights/member-of-new-navigation.md")
    assert not any(relation["target"].startswith("$new.") for relation in fm["relations"])


def test_merge_cannot_silently_absorb_different_focal_claim(vault):
    _finding(vault)
    _finding(vault, OTHER_FINDING, "other-finding", claim_key="different-finding")
    path = _proposal(vault, [{"op": "merge", "keep": FINDING, "merge": OTHER_FINDING}])
    _assert_rejected_without_writes(vault, path, "different owner knowledge claim identities")
