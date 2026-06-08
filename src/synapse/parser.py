"""Markdown entity parser."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from synapse.models import ENTITY_TYPES, Entity, Issue
from synapse.util import read_frontmatter, sha256_file

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def entity_files(vault: Path) -> list[Path]:
    root = vault / "entities"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def extract_wikilinks(body: str) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for match in WIKILINK_RE.finditer(body):
        ref = match.group(1).strip()
        key = ref.casefold()
        if ref and key not in seen:
            seen.add(key)
            refs.append(ref)
    return refs


class ParseError(ValueError):
    pass


def _parse_entity_file_internal(
    path: Path, vault: Path | None = None
) -> tuple[Entity | None, list[Issue]]:
    issues: list[Issue] = []
    try:
        frontmatter, body = read_frontmatter(path)
    except Exception as exc:
        return None, [Issue("error", f"Could not parse frontmatter: {exc}", path)]

    for field in ["id", "type", "name", "review_status"]:
        if not frontmatter.get(field):
            issues.append(Issue("error", f"Missing required field: {field}", path))

    entity_type = str(frontmatter.get("type", "")).strip()
    if entity_type and entity_type not in ENTITY_TYPES:
        issues.append(Issue("warning", f"Unknown entity type: {entity_type}", path))

    review_status = str(frontmatter.get("review_status", "proposed")).strip()
    if review_status not in {"proposed", "verified"}:
        issues.append(Issue("error", f"Invalid review_status: {review_status}", path))

    relations = frontmatter.get("relations") or []
    if not isinstance(relations, list):
        issues.append(Issue("error", "relations must be a list", path))
        relations = []

    properties = frontmatter.get("properties") or {}
    if not isinstance(properties, dict):
        issues.append(Issue("warning", "properties must be a map; ignoring", path))
        properties = {}

    if any(issue.severity == "error" for issue in issues):
        return None, issues

    file_path = path if vault is None else path.resolve().relative_to(vault.resolve())
    entity = Entity(
        id=str(frontmatter["id"]).strip(),
        type=entity_type,
        name=str(frontmatter["name"]).strip(),
        file_path=file_path,
        frontmatter=frontmatter,
        body=body,
        content_hash=sha256_file(path),
        review_status=review_status,  # type: ignore[arg-type]
        aliases=_string_list(frontmatter.get("aliases")),
        tags=_string_list(frontmatter.get("tags")),
        properties=properties,
        created_at=str(frontmatter.get("created_at") or "") or None,
        updated_at=str(frontmatter.get("updated_at") or "") or None,
        relation_specs=[item for item in relations if isinstance(item, dict)],
        weak_refs=extract_wikilinks(body),
    )
    return entity, issues


def parse_vault(vault: Path) -> tuple[list[Entity], list[Issue]]:
    entities: list[Entity] = []
    issues: list[Issue] = []
    seen_ids: dict[str, Path] = {}
    for path in entity_files(vault):
        entity, file_issues = _parse_entity_file_internal(path, vault)
        issues.extend(file_issues)
        if entity is None:
            continue
        if entity.id in seen_ids:
            issues.append(
                Issue("error", f"Duplicate entity id also found in {seen_ids[entity.id]}", path)
            )
            continue
        seen_ids[entity.id] = path
        entities.append(entity)
    return entities, issues


def parse_entity_file(path: Path, vault: Path | None = None) -> Entity:
    entity, issues = _parse_entity_file_internal(path, vault)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ParseError("; ".join(issue.message for issue in errors))
    if entity is None:
        raise ParseError(f"Could not parse entity file: {path}")
    return entity


parse_markdown_entity = parse_entity_file
parse_entity = parse_entity_file
