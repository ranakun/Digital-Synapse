"""Owner knowledge must preserve scope, evidence dependence and corrections."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from synapse import owner_context
from synapse.index import connect, reindex
from synapse.models import Entity
from synapse.owner_context import (
    KNOWLEDGE_PROFILE,
    build_owner_context,
    format_knowledge_notice,
    format_profile_entity,
    load_knowledge_entities,
    owner_context_pointer,
    validate_knowledge_graph,
    validate_knowledge_record,
)
from synapse.util import write_frontmatter

NAV = "01J00000000000000000000100"
FINDING = "01J00000000000000000000101"
EVIDENCE = "01J00000000000000000000102"
QUALIFIER = "01J00000000000000000000103"
LEGACY = "01J00000000000000000000104"
OTHER = "01J00000000000000000000105"
PROJECT = "01J00000000000000000000106"
NEW = "01J00000000000000000000107"


def props(kind="finding", key="learning.representation", **overrides):
    return {
        "knowledge_profile": KNOWLEDGE_PROFILE,
        "subject_id": "me",
        "record_kind": kind,
        "claim_key": key,
        "facets": ["learning"],
        "epistemic_basis": ["owner-report"],
        "owner_position": "stated",
        "lifecycle": "current",
        "as_of": "2026-09-01",
        "source_family_id": "session-1",
        **overrides,
    }


def body(statement="A useful reported pattern.", limits="This is a report, not a comparative measure.", evidence="The source describes this episode; locator U01."):
    return f"## Statement\n{statement}\n\n## Conditions and limits\n{limits}\n\n## Evidence\n{evidence}\n"


def rel(target, roles, **values):
    return {"type": "related_to", "target": target, "properties": {"roles": roles, "note": "Recorded connection.", **values}}


def entity(eid, *, properties=None, content=None, relations=None, name=None, etype="insight"):
    fm = {
        "id": eid, "type": etype, "name": name or eid,
        "review_status": "proposed", "properties": properties if properties is not None else props(),
        "relations": relations if relations is not None else [rel(NAV, ["member"])],
    }
    return Entity(
        id=eid, type=etype, name=fm["name"], file_path=Path(f"entities/insights/{eid}.md"),
        frontmatter=fm, body=content if content is not None else body(), content_hash="fixture",
        properties=fm["properties"], relation_specs=fm["relations"],
    )


def graph():
    return {
        "me": entity("me", properties={}, content="Owner.", relations=[], name="Owner", etype="person"),
        NAV: entity(NAV, properties=props("navigation", "owner.navigation", facets=["discovery"]), relations=[rel("me", ["about"])], name="Owner knowledge"),
        FINDING: entity(
            FINDING, name="Representation finding",
            content=body("SELECTED CLAIM: representation helps in this example.", "ESSENTIAL LIMIT: it does not measure general intelligence."),
            relations=[rel(NAV, ["member"]), rel(EVIDENCE, ["evidence"], source_locator="R3"), rel(PROJECT, ["applies_to"])],
        ),
        EVIDENCE: entity(
            EVIDENCE, name="Probability episode", etype="conversation",
            properties=props("evidence", "episode.probability", lifecycle="historical"),
            content=body("An answer used natural frequencies.", "The follow-up explanation was supplied after clarification.", "Verbatim answer at source U03."),
        ),
        QUALIFIER: entity(
            QUALIFIER, name="Scope qualification",
            properties=props(key="stopping.qualification", facets=["quality", "work"]),
            content=body("QUALIFICATION: this episode involved relevant new feedback.", "It does not remove the separate continuity account."),
            relations=[rel(NAV, ["member"]), rel(LEGACY, ["qualifies"], scope="Act 1 stopping account")],
        ),
        LEGACY: entity(LEGACY, properties={"as_of": "2026-07-27"}, name="Earlier commitment account", content="An older broad paragraph.", relations=[]),
        PROJECT: entity(PROJECT, properties={"as_of": "2026-09-01"}, name="Chosen project", content="Actual project terms.", relations=[], etype="project"),
        OTHER: entity(OTHER, properties={}, name="UNRELATED NETWORK PERSON", content="Should never appear.", relations=[{"type": "knows", "target": "me"}], etype="person"),
    }


def persist(vault, records):
    for item in records.values():
        write_frontmatter(vault / item.file_path, item.frontmatter, item.body)
    result = reindex(vault, full=True)
    assert not result.has_errors


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(owner_context, "_today", lambda: date(2026, 9, 15))


@pytest.fixture()
def vault(tmp_path):
    path = tmp_path / "vault"
    persist(path, graph())
    return path


@pytest.fixture()
def conn(vault):
    connection = connect(vault)
    yield connection
    connection.close()


def test_valid_profile_and_unprofiled_data_are_accepted():
    records = graph()
    assert validate_knowledge_graph(records) == []
    assert validate_knowledge_record({"type": "person", "properties": {"confidence": 0.8}}, "") == []


@pytest.mark.parametrize("change,expected", [
    ({"subject_id": "someone-else"}, "subject_id"),
    ({"as_of": "2026-02-30"}, "calendar"),
    ({"epistemic_basis": "owner-report"}, "epistemic_basis"),
    ({"owner_position": "diagnosed"}, "owner_position"),
    ({"facets": ["Bad Facet"]}, "facets"),
    ({"ability_score": 99}, "numeric ability/certainty"),
    ({"nested": {"claim": "x"}}, "flat"),
])
def test_malformed_profile_fields_fail(change, expected):
    record = entity(FINDING, properties=props(**change))
    assert any(expected in error for error in validate_knowledge_record(record.frontmatter, record.body))


def test_headings_inside_fences_do_not_fake_required_sections():
    record = entity(FINDING, content="```markdown\n" + body() + "```\n")
    assert "required Markdown section" in " ".join(validate_knowledge_record(record.frontmatter, record.body))
    record.body = body() + "\n## Statement\nA conflicting second statement."
    assert "more than once" in " ".join(validate_knowledge_record(record.frontmatter, record.body))


def test_projected_new_keys_are_resolved_without_ulid_assumptions():
    records = graph()
    nav = records.pop(NAV)
    records["$new.0"] = replace(nav, id="$new.0")
    for item in records.values():
        for relation in item.frontmatter.get("relations") or []:
            if relation["target"] == NAV:
                relation["target"] = "$new.0"
    finding = records.pop(FINDING)
    records["$new.1"] = replace(finding, id="$new.1")
    assert validate_knowledge_graph(records) == []


def test_roles_cannot_overwrite_each_other_or_dangle():
    records = graph()
    records[FINDING].frontmatter["relations"].append(rel(EVIDENCE, ["applies_to"]))
    assert "combine roles" in " ".join(issue.message for issue in validate_knowledge_graph(records))
    records[FINDING].frontmatter["relations"][-1] = rel("$new.missing", ["evidence"])
    assert "unresolved" in " ".join(issue.message for issue in validate_knowledge_graph(records))


def test_revisions_require_same_claim_key_and_explicit_scope():
    records = graph()
    records[QUALIFIER].frontmatter["relations"][-1] = rel(FINDING, ["revises"])
    errors = " ".join(issue.message for issue in validate_knowledge_graph(records))
    assert "explicit scope" in errors
    assert "same claim_key" in errors


def test_current_conflicts_and_revision_cycles_are_not_resolved_by_recency():
    records = graph()
    records[NEW] = entity(NEW, properties=props(as_of="2026-09-10"))
    errors = " ".join(issue.message for issue in validate_knowledge_graph(records))
    assert "conflicting current versions" in errors
    records[FINDING].frontmatter["properties"]["lifecycle"] = "historical"
    records[NEW].frontmatter["relations"].append(rel(FINDING, ["revises"], scope="Same focal proposition"))
    records[FINDING].frontmatter["relations"].append(rel(NEW, ["revises"], scope="Invalid reverse revision"))
    assert "revision cycle" in " ".join(issue.message for issue in validate_knowledge_graph(records))


def test_same_key_with_nonoverlapping_explicit_windows_is_valid():
    records = graph()
    records[FINDING].frontmatter["properties"]["applies_until"] = "2026-09-09"
    records[NEW] = entity(NEW, properties=props(applies_from="2026-09-10", as_of="2026-09-10"))
    assert validate_knowledge_graph(records) == []


def test_scope_and_evidence_basis_survive_normal_read(conn):
    text = build_owner_context(conn, facet="learning", target_id=PROJECT)
    assert "SELECTED CLAIM" in text and "ESSENTIAL LIMIT" in text
    assert f"`{EVIDENCE}`" in text
    assert "owner-report" in text and "owner: stated" in text
    assert "Source family: `session-1`" in text
    assert "not independent corroboration" in text
    assert "Chosen project" in text
    assert "UNRELATED NETWORK PERSON" not in text
    assert "Available facets:" in text
    assert "quality" in text


@pytest.mark.parametrize("budget", [1, 30, 150, 500, 1100, 8000])
def test_budget_never_separates_statement_and_limits(conn, budget):
    text = build_owner_context(conn, facet="learning", budget_chars=budget)
    assert len(text) <= budget
    assert ("SELECTED CLAIM" in text) == ("ESSENTIAL LIMIT" in text)
    detail = format_profile_entity(conn, FINDING, budget_chars=budget)
    assert detail is not None and len(detail) <= budget
    assert ("SELECTED CLAIM" in detail) == ("ESSENTIAL LIMIT" in detail)


def test_direct_legacy_entry_shows_qualification_with_whole_id(conn):
    notice = format_knowledge_notice(conn, LEGACY, budget_chars=450)
    assert "Qualified/revised" in notice and f"`{QUALIFIER}`" in notice
    assert len(notice) <= 450
    assert format_profile_entity(conn, LEGACY) is None
    assert format_knowledge_notice(conn, OTHER) == ""


def test_small_profile_notice_labels_basis_even_without_full_card(conn):
    text = format_knowledge_notice(conn, FINDING, budget_chars=600)
    assert "owner-report" in text
    assert "current" in text
    assert "conditions and limits" in text
    assert len(text) <= 600


def test_incoming_qualification_stored_on_legacy_destination_is_read(vault):
    records = graph()
    records[QUALIFIER].frontmatter["relations"].pop()
    incoming = rel(QUALIFIER, ["qualifies"], scope="Act 1 stopping account")
    incoming["direction"] = "incoming"
    records[LEGACY].frontmatter["relations"].append(incoming)
    persist(vault, records)
    connection = connect(vault)
    try:
        assert validate_knowledge_graph(load_knowledge_entities(connection)) == []
        assert f"`{QUALIFIER}`" in format_knowledge_notice(connection, LEGACY)
    finally:
        connection.close()


def test_graph_invalid_qualifier_is_an_alert_not_a_substantive_assertion(vault):
    records = graph()
    records[QUALIFIER].frontmatter["relations"] = [rel(FINDING, ["qualifies"], scope="Specific sample")]
    persist(vault, records)
    connection = connect(vault)
    try:
        text = format_profile_entity(connection, FINDING, budget_chars=5000)
        assert "Unvalidated correction" in text
        assert "QUALIFICATION:" not in text
        assert f"`{QUALIFIER}`" in text
        notice = format_knowledge_notice(connection, FINDING, budget_chars=5000)
        assert "unvalidated qualifies" in notice
        assert "QUALIFICATION:" not in notice
    finally:
        connection.close()


def test_explicit_facet_precedes_generic_collaboration(vault):
    records = graph()
    records[NEW] = entity(
        NEW, name="Generic collaboration", properties=props("preference", "aaa.collaboration", facets=["collaboration"]),
        content=body("GENERIC CORE", "Core limit."),
    )
    persist(vault, records)
    connection = connect(vault)
    try:
        text = build_owner_context(connection, facet="learning", budget_chars=9000)
        assert text.index("SELECTED CLAIM") < text.index("GENERIC CORE")
    finally:
        connection.close()


def test_current_and_historical_date_selection_respects_explicit_revision(vault):
    records = graph()
    records[FINDING].frontmatter["properties"]["lifecycle"] = "historical"
    records[NEW] = entity(
        NEW, name="Revised finding", properties=props(as_of="2026-09-10"),
        content=body("NEW FORMULATION", "NEW LIMIT"),
        relations=[rel(NAV, ["member"]), rel(FINDING, ["revises"], scope="Same focal proposition")],
    )
    persist(vault, records)
    connection = connect(vault)
    try:
        current = build_owner_context(connection, facet="learning", budget_chars=10000)
        assert "NEW FORMULATION" in current
        assert "SELECTED CLAIM" not in current
        earlier = build_owner_context(connection, facet="learning", as_of="2026-09-05", budget_chars=10000)
        assert "SELECTED CLAIM" in earlier and "NEW FORMULATION" not in earlier
    finally:
        connection.close()


def test_expired_and_withdrawn_findings_are_not_current(vault):
    records = graph()
    records[FINDING].frontmatter["properties"]["applies_until"] = "2026-09-09"
    records[QUALIFIER].frontmatter["properties"]["lifecycle"] = "withdrawn"
    persist(vault, records)
    connection = connect(vault)
    try:
        text = build_owner_context(connection, facet="learning")
        assert "SELECTED CLAIM" not in text
        assert format_knowledge_notice(connection, LEGACY) == ""
    finally:
        connection.close()


@pytest.mark.parametrize("kwargs, expected", [
    ({"facet": "not-a-known-facet"}, "Unknown facet"),
    ({"as_of": "2026-02-30"}, "calendar"),
    ({"as_of": "2026-9-1"}, "YYYY-MM-DD"),
    ({"target_id": "missing"}, "Unknown target"),
    ({"budget_chars": 0}, "positive integer"),
])
def test_bad_query_arguments_fail_clearly(conn, kwargs, expected):
    with pytest.raises(ValueError, match=expected):
        build_owner_context(conn, **kwargs)


def test_reads_are_query_only_and_rebuild_preserves_result(vault):
    connection = connect(vault)
    try:
        before = {str(path.relative_to(vault)): path.read_bytes() for path in (vault / "entities").rglob("*.md")}
        connection.execute("PRAGMA query_only = ON")
        text = build_owner_context(connection, facet="learning", as_of="2026-09-15")
        assert "owner-context" in owner_context_pointer(connection)
        format_knowledge_notice(connection, LEGACY)
        format_profile_entity(connection, FINDING)
        load_knowledge_entities(connection)
        after = {str(path.relative_to(vault)): path.read_bytes() for path in (vault / "entities").rglob("*.md")}
        assert before == after
    finally:
        connection.close()
    (vault / ".synapse" / "index.db").unlink()
    reindex(vault, full=True)
    rebuilt = connect(vault)
    try:
        assert build_owner_context(rebuilt, facet="learning", as_of="2026-09-15") == text
    finally:
        rebuilt.close()


def test_shared_source_reviews_remain_interpretations(vault):
    records = graph()
    records[NEW] = entity(
        NEW, name="Another review", properties=props(key="interpretation.learning", epistemic_basis=["assistant-hypothesis"], owner_position="unreviewed"),
        content=body("Another interpretation of the same episode.", "Not independent evidence for the underlying capability."),
        relations=[rel(NAV, ["member"]), rel(EVIDENCE, ["evidence"], source_family_id="session-1")],
    )
    persist(vault, records)
    connection = connect(vault)
    try:
        text = build_owner_context(connection, facet="learning", budget_chars=14000)
        assert "assistant-hypothesis" in text and "unreviewed" in text
        assert "Not independent evidence" in text
        assert text.count("Source family: `session-1`") >= 2
    finally:
        connection.close()


def test_invalid_incoming_roles_are_not_silently_accepted():
    records = graph()
    records[QUALIFIER].frontmatter["relations"].pop()
    incoming = rel(QUALIFIER, ["qualifies"], scope="Act 1")
    incoming["direction"] = "incoming"
    del incoming["properties"]["scope"]
    records[LEGACY].frontmatter["relations"].append(incoming)
    assert "explicit scope" in " ".join(issue.message for issue in validate_knowledge_graph(records))
    broken = copy.deepcopy(records)
    broken[LEGACY].frontmatter["relations"][-1]["properties"]["roles"] = "qualifies"
    assert "roles list" in " ".join(issue.message for issue in validate_knowledge_graph(broken))


def test_bad_correction_date_cannot_silently_remove_legacy_notice(vault):
    records = graph()
    records[QUALIFIER].frontmatter["properties"]["as_of"] = "invalid-date"
    persist(vault, records)
    connection = connect(vault)
    try:
        text = format_knowledge_notice(connection, LEGACY, budget_chars=2000)
        assert f"`{QUALIFIER}`" in text
        assert "unvalidated" in text
        assert "QUALIFICATION:" not in text
    finally:
        connection.close()
