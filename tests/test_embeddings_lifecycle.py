"""Tests for T4.1: Embedding lifecycle — incremental, composition, guards."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.embeddings import HashEmbedder, embed_entities, embedding_text, nearest_duplicates
from synapse.index import reindex  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENTITY_A = """\
---
id: test-entity-001
type: person
name: Alice Smith
review_status: proposed
tags:
  - recruiter
  - linkedin
relations: []
properties: {}
---
Alice is a person.
"""

_ENTITY_B = """\
---
id: test-entity-002
type: company
name: Acme Corp
review_status: proposed
tags: []
relations: []
properties: {}
---
Acme Corp is a company.
"""


@pytest.fixture()
def temp_vault(tmp_path: Path) -> Path:
    """Create a minimal vault with two entity Markdown files."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".synapse").mkdir()
    people_dir = vault / "entities" / "people"
    people_dir.mkdir(parents=True)
    companies_dir = vault / "entities" / "companies"
    companies_dir.mkdir(parents=True)

    (people_dir / "alice.md").write_text(_ENTITY_A, encoding="utf-8")
    (companies_dir / "acme.md").write_text(_ENTITY_B, encoding="utf-8")

    # Build the initial index
    reindex(vault)

    return vault


# ---------------------------------------------------------------------------
# T4.1 lifecycle tests
# ---------------------------------------------------------------------------


def test_fresh_vault_embeds_all(temp_vault: Path) -> None:
    """First embed call on a fresh vault should embed all entities."""
    result = embed_entities(temp_vault, embedder=HashEmbedder())
    assert result["embedded"] == 2
    assert result["skipped"] == 0
    assert result["total"] == 2


def test_immediate_rerun_skips_all(temp_vault: Path) -> None:
    """Calling embed_entities twice without changes should skip everything."""
    embed_entities(temp_vault, embedder=HashEmbedder())
    result = embed_entities(temp_vault, embedder=HashEmbedder())
    assert result["embedded"] == 0
    assert result["skipped"] == 2
    assert result["total"] == 2


def test_edit_one_file_embeds_exactly_one(temp_vault: Path) -> None:
    """After editing one entity's body, only that entity should be re-embedded."""
    # First embed — both get embedded
    embed_entities(temp_vault, embedder=HashEmbedder())

    # Edit the body of one entity file
    alice_path = temp_vault / "entities" / "people" / "alice.md"
    original = alice_path.read_text(encoding="utf-8")
    alice_path.write_text(original + "\nUpdated biography text.\n", encoding="utf-8")

    # Reindex to pick up the change, then re-embed
    reindex(temp_vault, full=False)
    result = embed_entities(temp_vault, embedder=HashEmbedder())

    assert result["embedded"] == 1
    assert result["skipped"] == 1
    assert result["total"] == 2


def test_force_all_reembeds(temp_vault: Path) -> None:
    """force_all=True should re-embed all entities regardless of stored hashes."""
    embed_entities(temp_vault, embedder=HashEmbedder())
    result = embed_entities(temp_vault, embedder=HashEmbedder(), force_all=True)
    assert result["embedded"] == 2
    assert result["skipped"] == 0
    assert result["total"] == 2


def test_nearest_duplicates_uses_stored_vectors(temp_vault: Path) -> None:
    pytest.importorskip("numpy")

    people_dir = temp_vault / "entities" / "people"
    people_dir.joinpath("alice-variant.md").write_text(
        _ENTITY_A.replace("test-entity-001", "test-entity-003").replace(
            "Alice Smith", "Alice S."
        ),
        encoding="utf-8",
    )
    skills_dir = temp_vault / "entities" / "skills"
    skills_dir.mkdir()
    for entity_id, name in (("test-skill-001", "Java"), ("test-skill-002", "JavaScript")):
        skills_dir.joinpath(f"{entity_id}.md").write_text(
            f"---\nid: {entity_id}\ntype: skill\nname: {name}\nreview_status: proposed\n"
            f"relations: []\n---\n{name}\n",
            encoding="utf-8",
        )
    reindex(temp_vault)

    class SameVectorEmbedder:
        model = "same-vector-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    embed_entities(temp_vault, embedder=SameVectorEmbedder())

    matches = nearest_duplicates(temp_vault, threshold=0.99)

    assert len(matches) == 1
    assert {matches[0]["a"], matches[0]["b"]} == {"test-entity-001", "test-entity-003"}
    assert matches[0]["score"] == 1.0


def test_composition_golden() -> None:
    """embedding_text should produce the canonical prefixed format."""
    row = {
        "type": "person",
        "tags": ["recruiter", "linkedin"],
        "name": "Alice",
        "body": "<!-- synapse:managed -->noise<!-- /synapse:managed --> real body",
    }
    result = embedding_text(row)
    assert result == "person | tags: recruiter, linkedin\nAlice\nreal body"


def test_full_reindex_preserves_embeddings(temp_vault: Path) -> None:
    """A full reindex must NOT wipe embeddings of entities that still exist.

    Regression: the `embeddings` table declares
    `entity_id REFERENCES entities(id) ON DELETE CASCADE`, and connect() sets
    `PRAGMA foreign_keys=ON`. `reset_index` DROPs the entities table, whose
    implicit row-delete used to cascade and wipe ALL embeddings on every full
    reindex — silently breaking semantic search until the next `embed`. Entity
    ULIDs are stable across a rebuild, so the embeddings stay valid and must
    survive.
    """
    from synapse.index import connect

    embed_entities(temp_vault, embedder=HashEmbedder())

    conn = connect(temp_vault)
    try:
        before = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert before == 2, "Expected 2 embeddings before reindex"
    finally:
        conn.close()

    # Full rebuild: drops+recreates entities. Must not cascade-wipe embeddings.
    reindex(temp_vault, full=True)

    conn = connect(temp_vault)
    try:
        after = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert after == 2, "Full reindex cascade-wiped surviving embeddings"
    finally:
        conn.close()


def test_orphan_embedding_prune(temp_vault: Path) -> None:
    """Deleting an entity file should purge its embedding (FIX-11 orphan prune)."""
    # First embed — both entities get embedded
    embed_entities(temp_vault, embedder=HashEmbedder())

    # Verify both embeddings exist in the database
    from synapse.index import connect
    conn = connect(temp_vault)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM embeddings WHERE entity_id IN (?, ?)",
                           ("test-entity-001", "test-entity-002")).fetchall()
        assert rows[0][0] == 2, "Expected 2 embeddings before deletion"

        # Delete one entity file
        alice_path = temp_vault / "entities" / "people" / "alice.md"
        alice_path.unlink()

        # Reindex to remove the entity from the index, then re-embed
        reindex(temp_vault, full=False)
        embed_entities(temp_vault, embedder=HashEmbedder())

        # Verify the orphaned embedding was pruned
        rows = conn.execute("SELECT COUNT(*) FROM embeddings WHERE entity_id = ?",
                           ("test-entity-001",)).fetchall()
        assert rows[0][0] == 0, "Expected orphaned embedding to be pruned"

        # Verify the remaining entity still has its embedding
        rows = conn.execute("SELECT COUNT(*) FROM embeddings WHERE entity_id = ?",
                           ("test-entity-002",)).fetchall()
        assert rows[0][0] == 1, "Expected remaining embedding to persist"
    finally:
        conn.close()
