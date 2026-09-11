from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from synapse.index import _relations_for_entities
from synapse.models import Entity

runner = CliRunner()


def test_direction_incoming_swap() -> None:
    # Set up two mock entities
    e1 = Entity(
        id="01ABC000000000000000000001",
        type="conversation",
        name="Conversation X",
        file_path=Path("entities/conversations/conversation-x.md"),
        frontmatter={"id": "01ABC000000000000000000001", "type": "conversation", "name": "Conversation X"},
        body="Body X",
        content_hash="hashX",
        review_status="proposed",
        relation_specs=[
            {
                "type": "participated_in",
                "target": "01ABC000000000000000000002",
                "direction": "incoming",
                "properties": {"source": "linkedin"},
                "source": "messages.csv",
                "created_at": "2026-06-11T12:00:00Z"
            }
        ]
    )
    e2 = Entity(
        id="01ABC000000000000000000002",
        type="person",
        name="Alice",
        file_path=Path("entities/people/alice.md"),
        frontmatter={"id": "01ABC000000000000000000002", "type": "person", "name": "Alice"},
        body="Body Alice",
        content_hash="hashAlice",
        review_status="proposed"
    )

    relations, issues = _relations_for_entities([e1, e2])
    assert len(issues) == 0
    assert len(relations) == 1
    rel = relations[0]
    
    # direction: incoming swaps from_id and to_id
    # declaring entity e1 id is 01ABC000000000000000000001
    # target is 01ABC000000000000000000002
    # So from_id becomes target (01ABC000000000000000000002) and to_id becomes declaring entity (01ABC000000000000000000001)
    assert rel.from_id == "01ABC000000000000000000002"
    assert rel.to_id == "01ABC000000000000000000001"
    assert rel.type == "participated_in"
    assert rel.weak is False
    assert rel.properties == {"source": "linkedin"}
    assert rel.source_file == "messages.csv"
    assert rel.created_at == "2026-06-11T12:00:00Z"


def test_direction_invalid_value() -> None:
    e1 = Entity(
        id="01ABC000000000000000000001",
        type="conversation",
        name="Conversation X",
        file_path=Path("entities/conversations/conversation-x.md"),
        frontmatter={"id": "01ABC000000000000000000001", "type": "conversation", "name": "Conversation X"},
        body="Body X",
        content_hash="hashX",
        review_status="proposed",
        relation_specs=[
            {
                "type": "participated_in",
                "target": "01ABC000000000000000000002",
                "direction": "invalid-direction"
            }
        ]
    )
    e2 = Entity(
        id="01ABC000000000000000000002",
        type="person",
        name="Alice",
        file_path=Path("entities/people/alice.md"),
        frontmatter={"id": "01ABC000000000000000000002", "type": "person", "name": "Alice"},
        body="Body Alice",
        content_hash="hashAlice",
        review_status="proposed"
    )

    relations, issues = _relations_for_entities([e1, e2])
    assert len(relations) == 0
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "Invalid relation direction" in issues[0].message


def test_relations_deduplication() -> None:
    # e1 declares an edge (e2 -> e1) using direction: incoming (e1 has smaller ID)
    # e2 declares the same edge (e2 -> e1) using direction: outgoing (e2 has larger ID)
    e1 = Entity(
        id="01ABC000000000000000000001",
        type="conversation",
        name="Conversation X",
        file_path=Path("entities/conversations/conversation-x.md"),
        frontmatter={"id": "01ABC000000000000000000001", "type": "conversation", "name": "Conversation X"},
        body="Body X",
        content_hash="hashX",
        review_status="proposed",
        relation_specs=[
            {
                "type": "participated_in",
                "target": "01ABC000000000000000000002",
                "direction": "incoming",
                "properties": {"prop1": "val1", "shared": "initial"},
                "source": "messages.csv",
                "created_at": "2026-06-11T12:00:00Z"
            }
        ]
    )
    e2 = Entity(
        id="01ABC000000000000000000002",
        type="person",
        name="Alice",
        file_path=Path("entities/people/alice.md"),
        frontmatter={"id": "01ABC000000000000000000002", "type": "person", "name": "Alice"},
        body="Body Alice",
        content_hash="hashAlice",
        review_status="proposed",
        relation_specs=[
            {
                "type": "participated_in",
                "target": "01ABC000000000000000000001",
                "direction": "outgoing",
                "properties": {"prop2": "val2", "shared": "overwrite"},
                "source": "other.csv",
                "created_at": "2026-06-11T13:00:00Z"
            }
        ]
    )

    # Note: e2 has larger ID, so when duplicate specs are processed:
    # 1. e1's spec is parsed (from_id=e2, to_id=e1)
    # 2. e2's spec is parsed (from_id=e2, to_id=e1)
    # The properties from e2 should overwrite/update e1.
    # Earliest non-empty created_at / source_file must be kept.
    relations, issues = _relations_for_entities([e2, e1])
    assert len(issues) == 0
    assert len(relations) == 1
    rel = relations[0]

    assert rel.from_id == "01ABC000000000000000000002"
    assert rel.to_id == "01ABC000000000000000000001"
    assert rel.type == "participated_in"
    assert rel.weak is False
    assert rel.properties == {"prop1": "val1", "prop2": "val2", "shared": "overwrite"}
    assert rel.source_file == "messages.csv"
    assert rel.created_at == "2026-06-11T12:00:00Z"


def test_weak_links_unaffected() -> None:
    e1 = Entity(
        id="01ABC000000000000000000001",
        type="conversation",
        name="Conversation X",
        file_path=Path("entities/conversations/conversation-x.md"),
        frontmatter={"id": "01ABC000000000000000000001", "type": "conversation", "name": "Conversation X"},
        body="Body X",
        content_hash="hashX",
        review_status="proposed",
        weak_refs=["Alice"]
    )
    e2 = Entity(
        id="01ABC000000000000000000002",
        type="person",
        name="Alice",
        file_path=Path("entities/people/alice.md"),
        frontmatter={"id": "01ABC000000000000000000002", "type": "person", "name": "Alice"},
        body="Body Alice",
        content_hash="hashAlice",
        review_status="proposed"
    )

    relations, issues = _relations_for_entities([e1, e2])
    assert len(issues) == 0
    assert len(relations) == 1
    rel = relations[0]
    assert rel.from_id == "01ABC000000000000000000001"
    assert rel.to_id == "01ABC000000000000000000002"
    assert rel.type == "mentioned_in"
    assert rel.weak is True
