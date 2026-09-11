"""Tests for synapse.guide — generated vault AGENTS.md."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.guide import generate_agent_guide, write_agent_guide
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


def _normalize(text: str) -> str:
    """Normalize timestamps and counts for stable comparison."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "TIMESTAMP", text)
    text = re.sub(r"\*\*\d+ entities\*\*", "**N entities**", text)
    return text


# ---------------------------------------------------------------------------
# T3.4 — Agent-guide golden test
# ---------------------------------------------------------------------------


def test_guide_has_all_sections(conn, vault: Path):
    text = generate_agent_guide(conn, vault)
    normalized = _normalize(text)

    # Section 1: What this vault is
    assert "What This Vault Is" in normalized
    # Section 2: Entity types
    assert "Entity Types" in normalized
    # Must list known entity types
    assert "person" in normalized
    assert "opportunity" in normalized
    assert "company" in normalized
    # Section 3: Conventions
    assert "Conventions" in normalized
    assert "ULID" in normalized or "ulid" in normalized.lower()
    # Section 4: Trust
    assert "Trust Semantics" in normalized
    assert "proposed" in normalized
    assert "verified" in normalized
    # Section 5: Query cookbook
    assert "Query Cookbook" in normalized
    assert "synapse find" in normalized
    assert "synapse neighbors" in normalized
    assert "synapse brief" in normalized
    # Section 6: Graph shape
    assert "Graph Shape" in normalized
    # Section 7: Citation
    assert "Citation" in normalized
    # Section 8: Feedback
    assert "Feedback" in normalized
    # Section 10: Question playbook (G1)
    assert "Question Playbook" in normalized
    # Section 11: Consuming vs developing (G1)
    assert "Consuming vs Developing" in normalized
    assert "USING-THE-BRAIN.md" in normalized
    # Shipped G2/G3 one-call surfaces must not regress to future-work guidance.
    assert "synapse_dossier" in normalized
    assert "synapse_warm_path" in normalized
    assert "G2 will make this one call" not in normalized
    assert "G3 will make this one call" not in normalized


def test_guide_has_no_provider_imports():
    """guide.py must not import any LLM provider module."""
    import importlib

    spec = importlib.util.find_spec("synapse.guide")
    assert spec is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert "openai" not in source
    assert "anthropic" not in source
    assert "providers" not in source


def test_guide_live_counts_present(conn, vault: Path):
    """Guide must include live entity counts from the index."""
    text = generate_agent_guide(conn, vault)
    # There are entities in the fixture vault; counts should be non-zero somewhere
    # The guide renders counts per entity type
    assert ": 1" in text or ": 2" in text or ": 3" in text


def test_guide_mentions_search_and_find(conn, vault: Path):
    """The discovery cookbook should retain both search and exact find."""
    text = generate_agent_guide(conn, vault)
    assert "search" in text.lower()
    assert "find" in text.lower()


def test_guide_trust_section_agents_never_set_verified(conn, vault: Path):
    """Guide trust section must state that agents never set verified."""
    text = generate_agent_guide(conn, vault)
    lower = text.lower()
    # Must note that only synapse verify sets verified
    assert "verify" in lower
    assert "never" in lower or "only" in lower


def test_write_agent_guide_creates_file(vault: Path):
    """write_agent_guide writes AGENTS.md in the vault root."""
    out = write_agent_guide(vault)
    assert out.exists()
    assert out.name == "AGENTS.md"
    content = out.read_text(encoding="utf-8")
    assert "Digital Synapse" in content
    assert "## 1. What This Vault Is" in content


def test_init_creates_agent_guide(tmp_path: Path):
    """synapse init CLI must generate AGENTS.md in the new vault."""
    from typer.testing import CliRunner

    from synapse.cli import app

    runner = CliRunner()
    vault = tmp_path / "new_vault"
    result = runner.invoke(app, ["init", "--path", str(vault), "--no-git"])
    assert result.exit_code == 0, f"init failed: {result.output}"
    guide = vault / "AGENTS.md"
    assert guide.exists(), "AGENTS.md should be created by synapse init"
    content = guide.read_text(encoding="utf-8")
    assert "Digital Synapse" in content
