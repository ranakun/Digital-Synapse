"""Deterministic structured-data importers."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.config import resolve_vault
from synapse.index import reindex
from synapse.parser import entity_files
from synapse.util import (
    generate_ulid,
    normalize_name,
    read_frontmatter,
    slugify,
    unique_path,
    utc_now,
    write_frontmatter,
)

LINKEDIN_IMPORTER = "deterministic:linkedin-connections"


@dataclass(frozen=True)
class LinkedInConnection:
    name: str
    first_name: str = ""
    last_name: str = ""
    profile_url: str = ""
    email: str = ""
    company: str = ""
    position: str = ""
    connected_on: str = ""


def _header_key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


_FIELD_ALIASES = {
    "first_name": {"firstname", "first", "givenname"},
    "last_name": {"lastname", "last", "surname", "familyname"},
    "name": {"name", "fullname", "full name"},
    "profile_url": {
        "url",
        "profileurl",
        "linkedinurl",
        "linkedinprofile",
        "publicprofileurl",
        "profilelink",
    },
    "email": {"email", "emailaddress", "emailaddress1", "primaryemail"},
    "company": {"company", "companyname", "organization", "organisation", "employer"},
    "position": {"position", "title", "jobtitle", "headline", "role"},
    "connected_on": {"connectedon", "connectiondate", "connecteddate", "dateconnected"},
}

_REQUIRED_HEADER_HINTS = {
    "firstname",
    "lastname",
    "fullname",
    "name",
    "url",
    "profileurl",
    "company",
    "position",
}


def _read_csv_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _rows_from_csv(text: str) -> tuple[list[list[str]], list[str]]:
    warnings: list[str] = []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
        warnings.append("Could not sniff CSV dialect; used comma-separated fallback.")
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    return rows, warnings


def _find_header_row(rows: list[list[str]]) -> tuple[int, list[str]]:
    best_index = -1
    best_score = 0
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        score = len(keys & _REQUIRED_HEADER_HINTS)
        if score > best_score:
            best_index = index
            best_score = score
    if best_index == -1 or best_score == 0:
        raise ValueError("Could not find a LinkedIn connections CSV header row.")
    return best_index, rows[best_index]


def _field_index(header: list[str]) -> dict[str, int]:
    by_key = {_header_key(value): index for index, value in enumerate(header)}
    indexes: dict[str, int] = {}
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            key = _header_key(alias)
            if key in by_key:
                indexes[field] = by_key[key]
                break
    return indexes


def _cell(row: list[str], indexes: dict[str, int], field: str) -> str:
    index = indexes.get(field)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def parse_linkedin_connections_csv(path: str | Path) -> tuple[list[LinkedInConnection], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]
    header_index, header = _find_header_row(rows)
    indexes = _field_index(header)
    if not indexes.get("name") and not (indexes.get("first_name") is not None or indexes.get("last_name") is not None):
        raise ValueError("LinkedIn CSV requires a name, first name, or last name column.")

    connections: list[LinkedInConnection] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            row = [*row, *[""] * (len(header) - len(row))]
            warnings.append(f"Row {row_number} had fewer columns than the header; padded blanks.")
        elif len(row) > len(header):
            row = row[: len(header)]
            warnings.append(f"Row {row_number} had extra columns; ignored trailing values.")

        first_name = _cell(row, indexes, "first_name")
        last_name = _cell(row, indexes, "last_name")
        full_name = _cell(row, indexes, "name") or " ".join(
            part for part in [first_name, last_name] if part
        ).strip()
        if not full_name:
            warnings.append(f"Row {row_number} skipped because it had no name.")
            continue
        connection = LinkedInConnection(
            name=full_name,
            first_name=first_name,
            last_name=last_name,
            profile_url=_cell(row, indexes, "profile_url"),
            email=_cell(row, indexes, "email"),
            company=_cell(row, indexes, "company"),
            position=_cell(row, indexes, "position"),
            connected_on=_cell(row, indexes, "connected_on"),
        )
        duplicate_key = (normalize_name(connection.name), normalize_name(connection.profile_url))
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate for {connection.name}.")
            continue
        seen.add(duplicate_key)
        connections.append(connection)
    return connections, warnings


def _entities_by_name(vault: Path) -> dict[tuple[str, str], Path]:
    results: dict[tuple[str, str], Path] = {}
    for path in entity_files(vault):
        metadata, _ = read_frontmatter(path)
        entity_type = str(metadata.get("type") or "")
        labels = [str(metadata.get("name") or ""), *[str(item) for item in metadata.get("aliases") or []]]
        for label in labels:
            normalized = normalize_name(label)
            if normalized:
                results.setdefault((entity_type, normalized), path)
    return results


def _entity_dir(vault: Path, entity_type: str) -> Path:
    folder = {
        "person": "people",
        "company": "companies",
        "project": "projects",
        "goal": "goals",
        "finance": "finance",
    }.get(entity_type, entity_type)
    return vault / "entities" / folder


def _merge_properties(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value not in {None, "", []} and not merged.get(key):
            merged[key] = value
    return merged


def _read_or_create_entity(
    vault: Path,
    by_name: dict[tuple[str, str], Path],
    *,
    entity_type: str,
    name: str,
    source_file: str,
    properties: dict[str, Any],
    body_append: str,
) -> tuple[str, Path, bool]:
    key = (entity_type, normalize_name(name))
    now = utc_now()
    path = by_name.get(key)
    if path:
        metadata, body = read_frontmatter(path)
        metadata["review_status"] = "proposed"
        metadata["updated_at"] = now
        metadata["properties"] = _merge_properties(metadata.get("properties") or {}, properties)
        metadata.setdefault("provenance", {}).update(
            {"source_file": source_file, "extracted_by": LINKEDIN_IMPORTER, "extracted_at": now}
        )
        if body_append and body_append not in body:
            body = f"{body.rstrip()}\n\n{body_append.strip()}\n"
        write_frontmatter(path, metadata, body)
        return str(metadata["id"]), path, False

    entity_id = generate_ulid()
    metadata = {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "aliases": [],
        "review_status": "proposed",
        "tags": ["linkedin"],
        "relations": [],
        "properties": {key: value for key, value in properties.items() if value not in {"", None}},
        "created_at": now,
        "updated_at": now,
        "provenance": {
            "source_file": source_file,
            "extracted_by": LINKEDIN_IMPORTER,
            "extracted_at": now,
        },
    }
    body = f"# {name}\n\n{body_append.strip()}".strip()
    path = unique_path(_entity_dir(vault, entity_type) / f"{slugify(name)}.md")
    write_frontmatter(path, metadata, body)
    by_name[key] = path
    return entity_id, path, True


def _add_relation(path: Path, relation: dict[str, Any]) -> None:
    metadata, body = read_frontmatter(path)
    relations = metadata.setdefault("relations", [])
    if not isinstance(relations, list):
        relations = []
        metadata["relations"] = relations
    for existing in relations:
        if (
            isinstance(existing, dict)
            and existing.get("type") == relation["type"]
            and existing.get("target") == relation["target"]
        ):
            properties = existing.setdefault("properties", {})
            if isinstance(properties, dict):
                properties.update({k: v for k, v in relation["properties"].items() if v})
            existing.setdefault("source", relation.get("source"))
            write_frontmatter(path, metadata, body)
            return
    relations.append(relation)
    write_frontmatter(path, metadata, body)


def import_linkedin_connections(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    connections, warnings = parse_linkedin_connections_csv(source_path)
    by_name = _entities_by_name(root)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    companies: dict[str, str] = {}

    for connection in connections:
        company_id = ""
        if connection.company:
            company_id, company_path, was_created = _read_or_create_entity(
                root,
                by_name,
                entity_type="company",
                name=connection.company,
                source_file=source_path.name,
                properties={},
                body_append=f"Imported from LinkedIn connections because {connection.name} listed this company.",
            )
            companies[connection.company] = company_id
            target_list = created if was_created else updated
            target_list.append({"id": company_id, "name": connection.company, "file_path": str(company_path.relative_to(root))})

        person_id, person_path, person_created = _read_or_create_entity(
            root,
            by_name,
            entity_type="person",
            name=connection.name,
            source_file=source_path.name,
            properties={
                "first_name": connection.first_name,
                "last_name": connection.last_name,
                "linkedin_url": connection.profile_url,
                "email": connection.email,
                "company": connection.company,
                "position": connection.position,
                "connected_on": connection.connected_on,
            },
            body_append="Imported from LinkedIn connections export.",
        )
        target_list = created if person_created else updated
        target_list.append({"id": person_id, "name": connection.name, "file_path": str(person_path.relative_to(root))})
        if company_id:
            _add_relation(
                person_path,
                {
                    "type": "works_at",
                    "target": company_id,
                    "properties": {"role": connection.position} if connection.position else {},
                    "source": source_path.name,
                },
            )

    reindex(root, full=True)
    return {
        "source": str(source_path),
        "importer": LINKEDIN_IMPORTER,
        "connections": len(connections),
        "companies": len(companies),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }
