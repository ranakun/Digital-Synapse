from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

parser = pytest.importorskip("synapse.parser")


FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
PERSON_PATH = FIXTURE_VAULT / "entities" / "people" / "example-person.md"


def call_parser(path: Path):
    for candidate in ("parse_entity_file", "parse_markdown_entity", "parse_entity"):
        func = getattr(parser, candidate, None)
        if callable(func):
            return func(path)
    pytest.fail("synapse.parser does not expose a parse_entity_file-style entry point")


def read_value(obj, *names):
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AssertionError(f"missing expected field(s): {names}")


def relation_target(value):
    if isinstance(value, str):
        return value
    return read_value(value, "target", "id", "name")


def test_parser_extracts_frontmatter_relations_and_wikilinks() -> None:
    result = call_parser(PERSON_PATH)

    assert read_value(result, "id") == "01J00000000000000000000001"
    assert read_value(result, "type") == "person"
    assert read_value(result, "name") == "Example Person"
    assert read_value(result, "review_status") == "verified"
    assert set(read_value(result, "aliases")) == {"Example P.", "EP"}

    relations = read_value(result, "relations")
    assert len(relations) == 3
    assert {relation_target(relation) for relation in relations} == {
        "01J00000000000000000000002",
        "01J00000000000000000000003",
        "01J00000000000000000000004",
    }

    wikilinks = read_value(result, "wikilinks", "weak_links", "body_links")
    link_values = {relation_target(link) for link in wikilinks}
    assert link_values == {
        "Example Company",
        "Example Project Alpha",
        "Example Project Beta",
    }


def test_parser_accepts_utf8_bom_before_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "bom-person.md"
    path.write_text(
        "\ufeff---\n"
        "id: 01J0000000000000000000BOM1\n"
        "type: person\n"
        "name: BOM Person\n"
        "review_status: verified\n"
        "relations: []\n"
        "---\n\n"
        "# BOM Person\n",
        encoding="utf-8",
    )

    result = call_parser(path)

    assert read_value(result, "name") == "BOM Person"


@pytest.mark.parametrize("missing_field", ["id", "type", "name", "review_status"])
def test_parser_rejects_missing_required_fields(tmp_path: Path, missing_field: str) -> None:
    bad_file = tmp_path / f"missing-{missing_field}.md"
    required = {
        "id": "01J000000000000000000000AA",
        "type": "person",
        "name": "Broken Entity",
        "review_status": "verified",
    }
    required.pop(missing_field)
    frontmatter = "\n".join(f"{key}: {value}" for key, value in required.items())
    bad_file.write_text(f"---\n{frontmatter}\n---\n\n# Broken Entity\n", encoding="utf-8")

    with pytest.raises(Exception, match=missing_field):
        call_parser(bad_file)
