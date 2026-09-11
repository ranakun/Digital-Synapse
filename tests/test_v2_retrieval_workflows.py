"""General retrieval regressions: no owner-specific expected answers."""

import sqlite3

import pytest
from test_v2_organization import _vault
from test_v2_read_view import _commit, _legacy, _record
from test_v2_semantic import SyntheticEmbedder, _store

from synapse.gateway import Gateway, transport_size
from synapse.organization import Organization
from synapse.source_purpose import write_source_purposes
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error
from synapse.v2_protocol import dispatch
from synapse.v2_semantic import SemanticRuntime, build_index, register_runtime


def test_relationship_pages_complete_busy_graph_and_filter_before_paging(tmp_path):
    center = generate_ulid()
    rows = [_legacy(center, name="Community", entity_type="company")]
    expected = set()
    for i in range(76):
        peer = generate_ulid()
        rows.append(_legacy(peer, name=f"Member {i}"))
        relation = "participated_in" if i % 3 == 0 else "works_at"
        edge = generate_ulid()
        rows.append(_record(edge, evidence=[], dependencies=[], context_refs=[], relationship={"from_id": peer, "to_id": center,
                                               "relation_type": relation, "scope": "Recorded episode"}))
        if relation == "participated_in":
            expected.add(edge)
    store = _commit(tmp_path / "vault", rows)
    head = store.head()
    seen, offset = set(), 0
    while True:
        value = dispatch(store.vault, "neighbors", {"id": center, "relation": "participated_in",
                         "direction": "in", "node_type": "person", "offset": offset, "limit": 12},
                         revision=head, budget_chars=7000)
        assert "error" not in value
        assert transport_size(value) <= 7000
        assert value["total_relationships"] == len(expected)
        ids = {e["id"] for e in value["edges"]}
        assert ids and not (seen & ids)
        seen |= ids
        if value["next_offset"] is None:
            break
        assert value["next_offset"] > offset
        offset = value["next_offset"]
    assert seen == expected
    assert store.head() == head
    assert Gateway(store.vault).neighbors(center, direction="out")["edges"] == []


def test_same_peer_parallel_relationships_have_continuation(tmp_path):
    left, right = generate_ulid(), generate_ulid()
    rows = [_legacy(left), _legacy(right)]
    for i in range(9):
        rows.append(_record(generate_ulid(), evidence=[], dependencies=[], context_refs=[], relationship={"from_id": left, "to_id": right,
                            "relation_type": "collaborated_with", "scope": f"Episode {i}"}))
    store = _commit(tmp_path / "vault", rows)
    gateway = Gateway(store.vault)
    first = gateway.neighbors(left, limit=3)
    second = gateway.neighbors(left, limit=3, offset=first["next_offset"])
    assert first["total_neighbors"] == second["total_neighbors"] == 1
    assert first["total_relationships"] == 9
    assert {e["id"] for e in first["edges"]}.isdisjoint(e["id"] for e in second["edges"])


def test_readiness_is_separate_from_usage_and_does_not_infer(tmp_path):
    store = _store(tmp_path)
    embedder = SyntheticEmbedder()
    gateway = Gateway(store.vault)
    assert gateway.describe()["semantic_capability"]["reason"] == "runtime-not-warmed"
    runtime = register_runtime(SemanticRuntime(store.vault, embedder))
    try:
        assert gateway.describe()["semantic_capability"]["state"] == "unavailable"
        build_index(store.vault, embedder=embedder)
        before = embedder.calls
        assert gateway.describe()["semantic_capability"]["state"] == "ready"
        assert gateway.catalog()["semantic_search"] == "unused"
        assert gateway.search_sources("alpha")["semantic_search"] == "unused"
        assert embedder.calls == before
        semantic = gateway.semantic("alpha")
        assert semantic["semantic_search"] == "ready"
        assert not any("did not use semantic" in note for note in semantic["limitations"])
        with sqlite3.connect(store.vault / ".synapse" / "v2-semantic" / f"{store.head()}.sqlite") as connection:
            connection.execute("UPDATE meta SET value='different-model' WHERE key='model'")
        assert gateway.describe()["semantic_capability"]["state"] == "stale"
    finally:
        runtime.close()


def test_owner_scope_recovers_boundary_without_question_words(tmp_path):
    owner = _legacy("me", name="Owner")
    boundary = _legacy(generate_ulid(), name="Availability", entity_type="insight",
                       properties={"subject_id": "me", "facets": ["planning"], "record_kind": "boundary"},
                       body="Evenings are reserved for family commitments.")
    project = _legacy(generate_ulid(), name="Course", entity_type="project", body="Complete the advanced mathematics course.")
    store = _commit(tmp_path / "vault", [owner, boundary, project])
    gateway = Gateway(store.vault)
    assert not gateway.context(query="mathematics")["items"][0]["qualified"]
    context = gateway.context(subject_id="me", facet="planning", knowledge_policy="current-state")
    assert context["items"][0]["root_id"] == boundary[0]["id"]
    assert "family" in context["items"][0]["records"][0]["body"]


def test_source_policy_invalidates_organization_without_losing_records(tmp_path):
    vault, store = _vault(tmp_path)
    gateway = Gateway(vault)
    organization = Organization(gateway)
    before = organization.snapshot()
    descriptor = next(iter(gateway.view.manifest["source_versions"].values()))
    assert any(member["kind"] == "source" for member in before["members"])
    write_source_purposes(vault, revision=store.head(), classifications=[{
        "source_id": descriptor["id"], "source_version": descriptor["version"],
        "purpose": "internal", "reason": "Synthetic operational artifact review",
    }])
    after = organization.snapshot()
    assert before["organization_revision"] != after["organization_revision"]
    assert after["coverage"]["excluded_internal_sources"] == 1
    assert all(member["kind"] != "source" for member in after["members"])
    assert {m["id"] for m in after["members"]} == {m["id"] for m in before["members"] if m["kind"] == "record"}
    with pytest.raises(V2Error, match="policy"):
        organization.snapshot(organization_revision=before["organization_revision"])
    assert gateway.search_sources("latency")["total"] == 0
    assert gateway.search_sources("latency", source_scope="all")["total"] > 0
    assert gateway.source(descriptor["id"])["text"]


def test_self_relationship_is_visible_in_both_direction_filters(tmp_path):
    identity, edge = generate_ulid(), generate_ulid()
    store = _commit(tmp_path / "vault", [_legacy(identity), _record(
        edge, evidence=[], dependencies=[], context_refs=[], relationship={
            "from_id": identity, "to_id": identity, "relation_type": "related_to", "scope": "Synthetic loop",
        })])
    gateway = Gateway(store.vault)
    for direction in ("both", "in", "out"):
        value = gateway.neighbors(identity, direction=direction)
        assert [row["id"] for row in value["edges"]] == [edge]
        assert value["total_relationships"] == 1
        assert {count["direction"] for count in value["relation_counts"]} == {"in", "out"}
