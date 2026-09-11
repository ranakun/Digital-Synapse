"""Tests for synapse.mcpserver formatters."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.index import connect, reindex
from synapse.mcpserver import (
    format_entity_card,
    format_neighbors,
    format_path,
    format_search_results,
    format_stats,
)
from synapse.web import build_meta, build_neighbors, build_path

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


def test_format_search_results() -> None:
    results = [
        {"id": "01", "name": "Alice", "type": "person", "snippet": "security expert"},
        {"id": "02", "name": "Acme", "type": "company", "snippet": "tech corp"},
        {
            "id": "03",
            "name": "Recruiter chat",
            "type": "conversation",
            "snippet": "responsive thread",
            "participants": [{"id": "person-03", "name": "Recruiter"}],
        },
    ]
    formatted = format_search_results(results)
    assert "participant: Recruiter `person-03`" in formatted
    assert "- [person] Alice `01` — security expert" in formatted
    assert "- [company] Acme `02` — tech corp" in formatted

    # Truncation test
    truncated = format_search_results(results, budget=80)
    assert "truncated" in truncated
    assert "Alice" in truncated


def test_format_stats(vault: Path, conn) -> None:
    meta_data = build_meta(conn, vault)
    formatted = format_stats(meta_data, conn)
    assert "Total Nodes:" in formatted
    assert "Entities by Type" in formatted
    # Truncation test
    truncated = format_stats(meta_data, conn, budget=50)
    assert "truncated" in truncated


def test_format_neighbors(vault: Path, conn) -> None:
    # 01J00000000000000000000001 is Example Person
    payload = build_neighbors(
        conn, vault, "01J00000000000000000000001", depth=1, types=None, undirected=True, include_weak=True, limit=150
    )
    formatted = format_neighbors("Example Person", "01J00000000000000000000001", payload)
    assert "Neighbors of **Example Person** `01J00000000000000000000001`" in formatted
    assert "works_at" in formatted
    assert "role: CTO" in formatted
    assert "source: sample-ingest.md" in formatted

    # Truncation test
    truncated = format_neighbors("Example Person", "01J00000000000000000000001", payload, budget=80)
    assert "truncated" in truncated


def test_format_entity_card(conn) -> None:
    row = conn.execute("SELECT * FROM entities WHERE id = '01J00000000000000000000001'").fetchone()
    from synapse.web import build_node
    node_data = build_node(conn, "01J00000000000000000000001")
    relations = node_data["relations"] if node_data else []
    
    formatted = format_entity_card(row, relations)
    assert "# Example Person `01J00000000000000000000001`" in formatted
    assert "Type: person" in formatted
    assert "Properties" in formatted
    assert "Relations" in formatted
    assert "Body" in formatted

    # Truncation test
    truncated = format_entity_card(row, relations, budget=100)
    assert "truncated" in truncated


def test_format_path(vault: Path, conn) -> None:
    # Path from me to company
    payload = build_path(conn, vault, "me", "01J00000000000000000000002", undirected=True, max_hops=4, include_weak=True)
    formatted = format_path(payload)
    assert "me" in formatted.lower()
    assert "has_goal" in formatted or "mentioned_in" in formatted
