"""Contract tests for the graph-UI HTTP API builder functions.

These exercise the pure ``build_*`` helpers in :mod:`synapse.web` directly
against the fixture vault, so they cover the JSON shapes, the neighbor cap,
and the reindex-once behaviour without standing up an HTTP server.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import queries, web
from synapse.index import connect, reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

# Stable fixture ids.
OWNER_ID = "me"
PERSON_ID = "01J00000000000000000000001"  # Example Person (works_at / leads / contributes_to)
GOAL_ID = "01J00000000000000000000005"  # Example Goal
MISSING_ID = "01J00000000000000000000999"


@pytest.fixture(scope="module")
def vault(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A reindexed, disposable copy of the fixture vault."""
    root = tmp_path_factory.mktemp("api") / "vault"
    shutil.copytree(FIXTURE_VAULT, root)
    reindex(root)
    return root


@pytest.fixture()
def conn(vault: Path):
    connection = connect(vault)
    try:
        yield connection
    finally:
        connection.close()


def test_build_meta_shape(conn, vault: Path) -> None:
    meta = web.build_meta(conn, vault)
    assert meta["owner_id"] == OWNER_ID
    counts = meta["counts"]
    assert counts["total_nodes"] > 0
    assert isinstance(counts["by_type"], dict)
    assert counts["by_type"].get("person", 0) >= 1
    assert counts["total_edges"] >= 1
    assert "person" in meta["entity_types"]
    assert meta["generated_at"].endswith("Z")
    # relation_types is a sorted-by-count list of {type, count, weak}.
    assert meta["relation_types"], "expected at least one relation type"
    for rt in meta["relation_types"]:
        assert set(rt) == {"type", "count", "weak"}
        assert isinstance(rt["weak"], bool)


def test_build_search_shape(conn, vault: Path) -> None:
    payload = web.build_search(conn, vault, "Example", 20)
    results = payload["results"]
    assert results, "expected search hits for 'Example'"
    keys = {"id", "name", "type", "review_status", "tags", "current_company"}
    for item in results:
        assert keys.issubset(item)
    assert any("Example" in item["name"] for item in results)


def test_build_node_relations_and_provenance(conn) -> None:
    payload = web.build_node(conn, PERSON_ID)
    assert payload is not None
    node = payload["node"]
    assert node["id"] == PERSON_ID
    assert node["type"] == "person"
    assert "provenance" in node  # may be None, but the key must exist
    rels = payload["relations"]
    assert rels, "Example Person should have relations"
    for rel in rels:
        assert rel["dir"] in {"in", "out"}
        assert {"id", "dir", "type", "weak", "properties", "other", "from_name", "to_name"}.issubset(rel)
        assert {"id", "name", "type", "review_status"}.issubset(rel["other"])
    # works_at is an outgoing typed relation on the fixture person.
    assert any(r["type"] == "works_at" and r["dir"] == "out" for r in rels)


def test_build_node_missing_returns_none(conn) -> None:
    assert web.build_node(conn, MISSING_ID) is None


def test_build_neighbors_default_includes_all_neighbors(conn, vault: Path) -> None:
    payload = web.build_neighbors(conn, vault, PERSON_ID, 1, None, True, False, 150)
    names = {n["name"] for n in payload["nodes"]}
    assert "Example Person" in names
    assert "Example Company" in names  # reached via works_at
    assert payload["truncated"] is False
    # every edge references only kept nodes
    kept = {n["id"] for n in payload["nodes"]}
    for edge in payload["edges"]:
        assert edge["from_id"] in kept and edge["to_id"] in kept
        assert "from_name" in edge
        assert "to_name" in edge


def test_build_neighbors_cap_truncates(conn, vault: Path) -> None:
    # Example Person has >1 neighbor; a limit of 1 must truncate.
    payload = web.build_neighbors(conn, vault, PERSON_ID, 1, None, True, False, 1)
    assert payload["truncated"] is True
    assert payload["total_neighbors"] >= 2
    # focus node + at most `limit` neighbors are kept
    assert len(payload["nodes"]) <= 2
    kept = {n["id"] for n in payload["nodes"]}
    assert PERSON_ID in kept
    for edge in payload["edges"]:
        assert edge["from_id"] in kept and edge["to_id"] in kept


def test_build_path_found_and_not_found(conn, vault: Path) -> None:
    found = web.build_path(conn, vault, PERSON_ID, GOAL_ID, True, 4, False)
    assert found["found"] is True
    assert found["hops"] >= 1
    assert len(found["nodes"]) >= 2
    for edge in found["edges"]:
        assert "from_name" in edge
        assert "to_name" in edge

    missing = web.build_path(conn, vault, PERSON_ID, MISSING_ID, True, 4, False)
    assert missing["found"] is False
    assert missing["hops"] == 0


def test_build_path_preserves_source_to_target_node_order(conn, vault: Path) -> None:
    found = web.build_path(conn, vault, GOAL_ID, PERSON_ID, True, 4, False)
    assert found["found"] is True
    assert [node["id"] for node in found["nodes"]][0] == GOAL_ID
    assert [node["id"] for node in found["nodes"]][-1] == PERSON_ID


def test_build_launcher_shape(conn) -> None:
    payload = web.build_launcher(conn)
    entities = payload["entities"]
    assert entities
    for ent in entities:
        assert {"id", "type", "name", "review_status"}.issubset(ent)


def test_builders_do_not_reindex(conn, vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The web layer must never reindex on a read request (reindex-once)."""
    calls = {"n": 0}

    def _counting_reindex(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1

    monkeypatch.setattr(queries, "_reindex", _counting_reindex)
    web.build_neighbors(conn, vault, PERSON_ID, 1, None, True, False, 150)
    web.build_search(conn, vault, "Example", 20)
    web.build_path(conn, vault, PERSON_ID, GOAL_ID, True, 4, False)
    assert calls["n"] == 0


def test_queries_reindex_flag(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reindex=False`` skips reindexing; the default still reindexes."""
    calls = {"n": 0}

    def _counting_reindex(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1

    monkeypatch.setattr(queries, "_reindex", _counting_reindex)

    queries.neighbors(vault, PERSON_ID, reindex=False)
    assert calls["n"] == 0

    queries.neighbors(vault, PERSON_ID, reindex=True)
    assert calls["n"] == 1
