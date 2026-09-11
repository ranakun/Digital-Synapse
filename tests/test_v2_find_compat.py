from __future__ import annotations

from pathlib import Path

from synapse.config import init_vault
from synapse.index import _insert_entity, connect, parse_vault, reindex, reset_index
from synapse.queries import find_entities
from synapse.util import write_frontmatter


def _seed_search_vault(root: Path) -> Path:
    vault = init_vault(root, initialize_git=False)
    for index in range(30):
        write_frontmatter(
            vault / "entities" / "people" / f"candidate-{index:02d}.md",
            {
                "id": f"synthetic-{index:02d}",
                "type": "person",
                "name": f"Shared Match {index:02d}",
                "review_status": "verified",
                "relations": [],
            },
            "Synthetic search fixture.",
        )
    reindex(vault, full=True)
    return vault


def _reinsert_entities(vault: Path, *, reverse: bool) -> None:
    entities, issues = parse_vault(vault)
    assert not issues
    conn = connect(vault, _skip_v2_refresh=True)
    try:
        reset_index(conn)
        with conn:
            for entity in reversed(entities) if reverse else entities:
                _insert_entity(conn, entity)
    finally:
        conn.close()


def _raw_fts_ids(vault: Path) -> list[str]:
    conn = connect(vault, _skip_v2_refresh=True)
    try:
        return [
            row["id"]
            for row in conn.execute(
                """
                SELECT e.id FROM entities_fts f
                JOIN entities e ON e.rowid = f.rowid
                WHERE entities_fts MATCH ?
                LIMIT 20
                """,
                ('"Shared" "Match"',),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_find_limit_is_stable_across_different_index_insertion_orders(tmp_path: Path) -> None:
    ascending = _seed_search_vault(tmp_path / "ascending")
    descending = _seed_search_vault(tmp_path / "descending")
    _reinsert_entities(ascending, reverse=False)
    _reinsert_entities(descending, reverse=True)
    assert _raw_fts_ids(ascending) != _raw_fts_ids(descending)
    expected = [f"synthetic-{index:02d}" for index in range(20)]

    for query in ("Shared Match", "Shared Missing"):
        ascending_ids = [row["id"] for row in find_entities(ascending, query, limit=20, reindex=False)]
        descending_ids = [row["id"] for row in find_entities(descending, query, limit=20, reindex=False)]
        assert ascending_ids == expected
        assert descending_ids == expected
