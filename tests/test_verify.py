"""Tests for synapse verify command and verify_entities / verify_report functions."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.maintenance import AmbiguousRefError, verify_entities, verify_report
from synapse.util import read_frontmatter, write_frontmatter

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    return v


@pytest.fixture()
def vault_with_proposed(vault: Path) -> Path:
    """Add a proposed person entity for verification tests."""
    people_dir = vault / "entities" / "people"
    path = people_dir / "proposed-person.md"
    write_frontmatter(
        path,
        {
            "id": "01J000000000000000000000FF",
            "type": "person",
            "name": "Proposed Person",
            "aliases": [],
            "review_status": "proposed",
            "tags": ["test-tag"],
            "relations": [],
            "properties": {},
        },
        "# Proposed Person\n\nThis person is proposed.",
    )
    return vault


# ---------------------------------------------------------------------------
# T3.5 — Single ref verification
# ---------------------------------------------------------------------------


def test_verify_single_ref_by_id(vault_with_proposed: Path) -> None:
    result = verify_entities(
        ["01J000000000000000000000FF"],
        vault_with_proposed,
    )
    assert result["requires_yes"] is False
    assert len(result["verified"]) == 1
    assert result["verified"][0]["id"] == "01J000000000000000000000FF"
    # Check that the file was actually updated
    path = vault_with_proposed / "entities" / "people" / "proposed-person.md"
    metadata, _ = read_frontmatter(path)
    assert metadata["review_status"] == "verified"
    assert metadata.get("updated_at")


def test_verify_already_verified_entity(vault: Path) -> None:
    # me.md is already verified
    result = verify_entities(["me"], vault)
    assert result["requires_yes"] is False
    assert len(result["already_verified"]) == 1
    assert result["already_verified"][0]["id"] == "me"
    assert len(result["verified"]) == 0


def test_verify_not_found_ref(vault: Path) -> None:
    result = verify_entities(["nonexistent-entity-xyz"], vault)
    assert "nonexistent-entity-xyz" in result["not_found"]
    assert result["requires_yes"] is False


# ---------------------------------------------------------------------------
# Batch verification with --yes
# ---------------------------------------------------------------------------


def test_verify_batch_requires_yes(vault_with_proposed: Path) -> None:
    """Batch flags (--type or --tag) require --yes to proceed."""
    result = verify_entities([], vault_with_proposed, type_filter="person", yes=False)
    assert result["requires_yes"] is True
    assert "candidates" in result
    assert len(result["candidates"]) > 0


def test_verify_batch_with_yes(vault_with_proposed: Path) -> None:
    result = verify_entities(
        [],
        vault_with_proposed,
        tag_filter="test-tag",
        yes=True,
    )
    assert result["requires_yes"] is False
    assert any(e["id"] == "01J000000000000000000000FF" for e in result["verified"])


def test_verify_batch_by_type_with_yes(vault_with_proposed: Path) -> None:
    result = verify_entities(
        [],
        vault_with_proposed,
        type_filter="skill",
        yes=True,
    )
    assert result["requires_yes"] is False
    # All skill entities should have been processed
    verified_ids = {e["id"] for e in result["verified"] + result["already_verified"]}
    assert "01J00000000000000000000009" in verified_ids  # Example Skill


# ---------------------------------------------------------------------------
# Ambiguity refusal
# ---------------------------------------------------------------------------


def test_verify_ambiguous_ref_raises(vault: Path) -> None:
    """When a ref is ambiguous, raise AmbiguousRefError with candidates."""
    # Add two people with the same name to create ambiguity
    people_dir = vault / "entities" / "people"
    for i, eid in enumerate(["01J0000000000000000000A001", "01J0000000000000000000A002"]):
        write_frontmatter(
            people_dir / f"duplicate-name-{i}.md",
            {
                "id": eid,
                "type": "person",
                "name": "Duplicate Name",
                "aliases": [],
                "review_status": "proposed",
                "tags": [],
                "relations": [],
                "properties": {},
            },
            "# Duplicate Name\n\n",
        )

    with pytest.raises(AmbiguousRefError) as exc_info:
        verify_entities(["Duplicate Name"], vault)
    assert exc_info.value.ref == "Duplicate Name"
    assert len(exc_info.value.candidates) >= 2


# ---------------------------------------------------------------------------
# Verify report
# ---------------------------------------------------------------------------


def test_verify_report_counts(vault_with_proposed: Path) -> None:
    report = verify_report(vault_with_proposed)
    assert "counts_by_type" in report
    counts = report["counts_by_type"]
    # person type should have both proposed and verified
    assert "person" in counts
    person_counts = counts["person"]
    assert person_counts.get("verified", 0) > 0  # me.md is verified
    assert person_counts.get("proposed", 0) > 0  # proposed-person.md


def test_verify_report_pending_advisories_absent(vault: Path) -> None:
    """No proposals/applied/ dir → empty advisories list."""
    report = verify_report(vault)
    assert report["pending_recommend_verify"] == []


def test_verify_report_pending_advisories_present(vault: Path) -> None:
    """Proposals with recommend_verify should appear in the report."""
    import yaml

    applied_dir = vault / "proposals" / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)
    advisory_file = applied_dir / "rec-001.yaml"
    advisory_file.write_text(
        yaml.safe_dump({"recommend_verify": "Verify the owner's positions", "applied_at": "2026-06-01"}),
        encoding="utf-8",
    )

    report = verify_report(vault)
    advisories = report["pending_recommend_verify"]
    assert len(advisories) == 1
    assert "rec-001.yaml" in advisories[0]["file"]
    assert "Verify" in advisories[0]["recommend_verify"]


# ---------------------------------------------------------------------------
# Invariant: only verify path sets review_status = "verified"
# ---------------------------------------------------------------------------


def test_only_verify_path_sets_verified_status() -> None:
    """
    Grep-style test: no module outside of verify/tests assigns review_status 'verified'.
    This guards against Phase 5 apply accidentally setting it.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "synapse"

    # Only maintenance.py (the verify command) is allowed to set review_status: verified.
    allowed = {"maintenance.py"}

    violations: list[str] = []
    for py_file in src_root.glob("*.py"):
        if py_file.name in allowed:
            continue
        text = py_file.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            # Look for assignments of review_status = "verified"
            if "verified" in line and "review_status" in line and "=" in line:
                # Exclude comments and string literals that are just checks/comparisons
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Allow comparisons (==, !=, in, not in)
                if "==" in line or "!=" in line or "' ==" in line or '" ==' in line:
                    continue
                # Allow .get(...) == "verified" patterns
                if ".get" in line and "verified" in line:
                    continue
                violations.append(f"{py_file.name}:{i}: {stripped}")

    assert violations == [], (
        "Found unexpected review_status='verified' assignment(s) outside maintenance.py:\n"
        + "\n".join(violations)
    )
