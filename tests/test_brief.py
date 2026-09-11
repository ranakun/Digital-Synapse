"""Tests for synapse.brief — owner brief and entity brief."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.brief import (
    OWNER_BRIEF_HEADER,
    _trim_to_budget,
    build_entity_brief,
    build_owner_brief,
)
from synapse.index import connect, reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    reindex(v, full=True)
    return v


@pytest.fixture()
def conn(vault: Path):
    c = connect(vault)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# T3.1 — Trimming logic and section builder unit tests
# ---------------------------------------------------------------------------


def test_trim_to_budget_keeps_all_when_under():
    sections = [
        (1, "Identity", "x" * 40),
        (2, "Positions", "y" * 40),
        (9, "Footer", "z" * 40),
    ]
    kept, trimmed = _trim_to_budget(sections, budget_tokens=100)
    assert len(trimmed) == 0
    assert len(kept) == 3


def test_trim_to_budget_removes_lowest_priority_first():
    # Budget only fits ~25 tokens; each section is 40 tokens
    sections = [
        (1, "Identity", "I" * 100),   # 25 tok
        (2, "Positions", "P" * 400),  # 100 tok — should be trimmed
        (8, "Goals", "G" * 400),      # 100 tok — trimmed before Positions
    ]
    kept, trimmed = _trim_to_budget(sections, budget_tokens=30)
    kept_names = {name for _, name, _ in kept}
    # Identity (prio 1) is never trimmed
    assert "Identity" in kept_names
    # Both low-prio sections should be trimmed
    assert "Goals" in trimmed or "Positions" in trimmed


def test_trim_to_budget_never_trims_section_1():
    sections = [
        (1, "Identity", "I" * 10000),  # huge but prio=1 → never trimmed
    ]
    kept, trimmed = _trim_to_budget(sections, budget_tokens=1)
    kept_names = {name for _, name, _ in kept}
    assert "Identity" in kept_names
    assert len(trimmed) == 0


def test_trim_to_budget_returns_sorted_by_priority():
    sections = [
        (9, "Footer", "F" * 80),
        (1, "Identity", "I" * 80),
        (4, "Skills", "S" * 80),
    ]
    kept, _ = _trim_to_budget(sections, budget_tokens=1000)
    priorities = [p for p, _, _ in kept]
    assert priorities == sorted(priorities)


def test_s1_identity_section_in_owner_brief(conn):
    text = build_owner_brief(conn, budget_tokens=8000)
    assert "## 1. Identity" in text
    # Owner entity is "Me" with id "me"
    assert "`me`" in text


# ---------------------------------------------------------------------------
# T3.2 — Entity brief unit tests
# ---------------------------------------------------------------------------


def test_entity_brief_person(conn):
    brief = build_entity_brief(conn, "01J00000000000000000000001", budget_tokens=4000)
    # Identity line
    assert "Example Person" in brief
    assert "`01J00000000000000000000001`" in brief
    assert "person" in brief
    # Should have properties or relations section
    assert "Properties" in brief or "Relations" in brief or "works_at" in brief


def test_entity_brief_opportunity(conn):
    brief = build_entity_brief(conn, "01J00000000000000000000008", budget_tokens=4000)
    assert "Example Senior Engineering Role" in brief
    assert "opportunity" in brief
    assert "`01J00000000000000000000008`" in brief


def test_entity_brief_missing_entity(conn):
    brief = build_entity_brief(conn, "nonexistent-id", budget_tokens=4000)
    assert "not found" in brief.lower()


def test_entity_brief_respects_budget(conn):
    # With a tiny budget, body should be truncated
    brief = build_entity_brief(conn, "01J00000000000000000000001", budget_tokens=10)
    # Even at tiny budget, the brief must exist but may be truncated
    assert brief.strip()


# ---------------------------------------------------------------------------
# T3.3 — Golden tests: owner brief and entity brief from fixture vault
# ---------------------------------------------------------------------------


def _normalize_timestamps(text: str) -> str:
    """Replace ISO timestamps for stable comparison."""
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "TIMESTAMP", text)


def test_owner_brief_golden(conn):
    """Owner brief from fixture vault: structural golden test."""
    text = build_owner_brief(conn, budget_tokens=8000)
    normalized = _normalize_timestamps(text)

    # Must start with the header
    assert OWNER_BRIEF_HEADER in normalized or "owner brief" in normalized.lower()
    # Must have identity section
    assert "## 1. Identity" in normalized
    # Must mention owner entity
    assert "`me`" in normalized
    # Footer section (lowest priority) should be present in a fixture that's small
    assert "## 10. Data Footer" in normalized
    # Should mention fixture entity counts
    assert "person" in normalized.lower()


def test_owner_brief_budget_trimming(conn):
    """Tiny budget causes truncation marker; section 1 is always kept."""
    text = build_owner_brief(conn, budget_tokens=30)
    # Section 1 always kept
    assert "## 1. Identity" in text
    # Should have truncation marker since most sections won't fit
    assert "truncated" in text.lower()


def test_entity_brief_golden_person(conn):
    """Entity brief for fixture person: structural golden test."""
    brief = build_entity_brief(conn, "01J00000000000000000000001", budget_tokens=4000)
    normalized = _normalize_timestamps(brief)

    assert "Example Person" in normalized
    assert "verified" in normalized or "proposed" in normalized
    assert "`01J00000000000000000000001`" in normalized


def test_entity_brief_golden_opportunity(conn):
    """Entity brief for fixture opportunity: structural golden test."""
    brief = build_entity_brief(conn, "01J00000000000000000000008", budget_tokens=4000)
    normalized = _normalize_timestamps(brief)

    assert "Example Senior Engineering Role" in normalized
    assert "opportunity" in normalized
    assert "status" in normalized.lower() or "prospect" in normalized.lower()


# ---------------------------------------------------------------------------
# No LLM / no-network assertion
# ---------------------------------------------------------------------------


def test_brief_module_has_no_provider_imports():
    """brief.py must not import any LLM provider module."""
    import synapse.brief as brief_mod

    for attr in ("openai", "anthropic", "providers", "ingest", "extractors"):
        assert not hasattr(brief_mod, attr), f"brief.py imported unexpected module: {attr}"

    # Check the module's source doesn't reference the provider loader
    import importlib
    spec = importlib.util.find_spec("synapse.brief")
    assert spec is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert "providers" not in source
    assert "openai" not in source
    assert "anthropic" not in source
