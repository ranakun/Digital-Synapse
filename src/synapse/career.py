"""Deterministic career-network reporting."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from synapse.index import all_entities, all_relations, connect, reindex

CAREER_ENTITY_TYPES = {
    "person",
    "company",
    "opportunity",
    "conversation",
    "event",
    "skill",
    "insight",
}
RECRUITER_KEYWORDS = {
    "recruiter",
    "talent",
    "sourcing",
    "people",
    "hr",
    "hiring",
    "acquisition",
}


def _properties(entity: dict[str, Any]) -> dict[str, Any]:
    value = entity.get("properties") or {}
    return value if isinstance(value, dict) else {}


def _tags(entity: dict[str, Any]) -> set[str]:
    return {str(tag).casefold() for tag in entity.get("tags") or []}


def _entity_label(entity: dict[str, Any] | None) -> str:
    if not entity:
        return "Unknown"
    return str(entity.get("name") or entity.get("id") or "Unknown")


def _is_owner(entity: dict[str, Any]) -> bool:
    return entity.get("id") == "me" or "owner" in _tags(entity)


def _is_recruiter(entity: dict[str, Any]) -> bool:
    props = _properties(entity)
    haystack = " ".join(
        str(value)
        for value in [
            entity.get("name"),
            props.get("current_role"),
            props.get("role"),
            props.get("current_company"),
            props.get("company"),
            " ".join(entity.get("tags") or []),
        ]
        if value
    ).casefold()
    return any(keyword in haystack for keyword in RECRUITER_KEYWORDS)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in [text[:10], text]:
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _line_items(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["_None found._"]


def _people_by_relation_company(
    entities_by_id: dict[str, dict[str, Any]], relations: list[dict[str, Any]]
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for rel in relations:
        if rel["type"] != "works_at":
            continue
        person = entities_by_id.get(str(rel["from_id"]))
        company = entities_by_id.get(str(rel["to_id"]))
        if not person or not company or person.get("type") != "person":
            continue
        if _is_owner(person):
            continue
        role = _properties(person).get("current_role") or rel.get("properties", {}).get("role")
        suffix = f" - {role}" if role else ""
        grouped[_entity_label(company)].append(f"{_entity_label(person)}{suffix}")
    return grouped


def _relation_neighbors(
    entity_id: str,
    entities_by_id: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    entity_type: str,
) -> list[str]:
    neighbors = []
    for rel in relations:
        linked_id = None
        if str(rel["from_id"]) == entity_id:
            linked_id = str(rel["to_id"])
        elif str(rel["to_id"]) == entity_id:
            linked_id = str(rel["from_id"])
        if not linked_id:
            continue
        entity = entities_by_id.get(linked_id)
        if entity and entity.get("type") == entity_type:
            neighbors.append(_entity_label(entity))
    return sorted(set(neighbors))


def generate_career_report(
    vault: str | Path | None = None,
    *,
    today: date | None = None,
) -> str:
    root = Path(vault or ".").resolve()
    reindex(root)
    current_date = today or datetime.now(UTC).date()
    conn = connect(root)
    try:
        entities = all_entities(conn)
        relations = all_relations(conn, include_weak=True)
    finally:
        conn.close()

    entities_by_id = {str(entity["id"]): entity for entity in entities}
    people = [
        entity for entity in entities if entity.get("type") == "person" and not _is_owner(entity)
    ]
    companies = [entity for entity in entities if entity.get("type") == "company"]
    opportunities = [entity for entity in entities if entity.get("type") == "opportunity"]
    owner_ids = {str(entity["id"]) for entity in entities if _is_owner(entity)}

    recruiters = []
    for person in sorted(people, key=lambda row: (_entity_label(row), str(row.get("id", "")))):
        if _is_recruiter(person):
            props = _properties(person)
            company = props.get("current_company") or props.get("company") or "Unknown company"
            role = props.get("current_role") or props.get("role") or "Unknown role"
            recruiters.append(f"{_entity_label(person)} - {role} at {company}")

    people_by_company: dict[str, list[str]] = defaultdict(list)
    for person in people:
        props = _properties(person)
        company = props.get("current_company") or props.get("company")
        if company:
            role = props.get("current_role") or props.get("role")
            suffix = f" - {role}" if role else ""
            people_by_company[str(company)].append(f"{_entity_label(person)}{suffix}")
    relation_companies = _people_by_relation_company(entities_by_id, relations)
    for company, contacts in relation_companies.items():
        people_by_company[company].extend(contacts)

    company_lines = []
    for company in sorted(people_by_company):
        contacts = sorted(set(people_by_company[company]))
        company_lines.append(f"{company}: {', '.join(contacts)}")

    known_contact_lines = []
    for company in sorted(companies, key=lambda row: (_entity_label(row), str(row.get("id", "")))):
        contacts = sorted(set(relation_companies.get(_entity_label(company), [])))
        if contacts:
            known_contact_lines.append(f"{_entity_label(company)}: {', '.join(contacts)}")

    opportunity_lines = []
    for opportunity in sorted(opportunities, key=lambda row: (_entity_label(row), str(row.get("id", "")))):
        props = _properties(opportunity)
        status = props.get("status") or "unknown"
        role = props.get("role") or opportunity.get("name")
        company = props.get("company") or "Unknown company"
        people_links = _relation_neighbors(str(opportunity["id"]), entities_by_id, relations, "person")
        skill_links = _relation_neighbors(str(opportunity["id"]), entities_by_id, relations, "skill")
        details = [f"status: {status}", f"company: {company}"]
        if role:
            details.append(f"role: {role}")
        if people_links:
            details.append("people: " + ", ".join(people_links))
        if skill_links:
            details.append("skills: " + ", ".join(skill_links))
        opportunity_lines.append(f"{_entity_label(opportunity)} ({'; '.join(details)})")

    owner_goal_lines = []
    for rel in relations:
        if str(rel["from_id"]) not in owner_ids or rel["type"] not in {"has_goal", "targets"}:
            continue
        target = entities_by_id.get(str(rel["to_id"]))
        if target and target.get("type") in {"goal", "opportunity", "skill", "company"}:
            owner_goal_lines.append(f"{_entity_label(target)} ({target['type']})")

    owner_position_items = []
    for rel in relations:
        if str(rel["from_id"]) not in owner_ids or rel["type"] not in {
            "works_at",
            "former_employee_of",
        }:
            continue
        company = entities_by_id.get(str(rel["to_id"]))
        if not company or company.get("type") != "company":
            continue
        props = rel.get("properties") or {}
        positions = props.get("positions") if isinstance(props, dict) else []
        if isinstance(positions, list) and positions:
            for position in positions:
                if not isinstance(position, dict):
                    continue
                title = position.get("title") or props.get("role") or "Unknown role"
                start = position.get("started_on") or "unknown"
                finish = position.get("finished_on") or "present"
                details = [f"{start} to {finish}"]
                location = position.get("location")
                skills = position.get("skills")
                if location:
                    details.append(f"location: {location}")
                if isinstance(skills, list) and skills:
                    details.append("skills: " + ", ".join(str(skill) for skill in skills))
                owner_position_items.append(
                    (
                        str(position.get("started_on") or ""),
                        f"{title} at {_entity_label(company)} ({'; '.join(details)})",
                    )
                )
        else:
            role = props.get("role") or "Unknown role"
            start = props.get("started_on") or "unknown"
            finish = props.get("finished_on") or "present"
            owner_position_items.append(
                (str(props.get("started_on") or ""), f"{role} at {_entity_label(company)} ({start} to {finish})")
            )
    owner_position_lines = [
        line for _, line in sorted(set(owner_position_items), key=lambda item: item[0], reverse=True)
    ]

    owner_skill_lines = []
    for rel in relations:
        if str(rel["from_id"]) not in owner_ids or rel["type"] != "demonstrates_skill":
            continue
        target = entities_by_id.get(str(rel["to_id"]))
        if not target or target.get("type") != "skill":
            continue
        props = rel.get("properties") or {}
        details = []
        authority = props.get("credential_authority")
        started_on = props.get("credential_started_on")
        finished_on = props.get("credential_finished_on")
        license_number = props.get("credential_license_number")
        if authority:
            details.append(f"authority: {authority}")
        if started_on:
            details.append(f"started: {started_on}")
        if finished_on:
            details.append(f"finished: {finished_on}")
        if license_number:
            details.append(f"license: {license_number}")
        suffix = f" ({'; '.join(details)})" if details else ""
        owner_skill_lines.append(f"{_entity_label(target)}{suffix}")

    warnings = []
    for person in sorted(people, key=lambda row: (_entity_label(row), str(row.get("id", "")))):
        props = _properties(person)
        if not (props.get("current_company") or props.get("company")):
            warnings.append(f"{_entity_label(person)} is missing current_company.")
        if not (props.get("current_role") or props.get("role")):
            warnings.append(f"{_entity_label(person)} is missing current_role.")
        for field in ["last_contacted_on", "last_interaction_on", "last_contact_date"]:
            parsed = _parse_date(props.get(field))
            if parsed and (current_date - parsed).days > 180:
                warnings.append(f"{_entity_label(person)} has stale {field}: {parsed.isoformat()}.")

    connected_ids = {
        str(entity_id)
        for rel in relations
        for entity_id in (rel["from_id"], rel["to_id"])
    }
    orphan_lines = [
        f"{_entity_label(entity)} ({entity['type']})"
        for entity in sorted(entities, key=lambda item: (_entity_label(item), str(item.get("id"))))
        if entity.get("type") in CAREER_ENTITY_TYPES
        and str(entity.get("id")) not in connected_ids
        and not _is_owner(entity)
    ]

    report = [
        "# Career Network Report",
        "",
        f"Generated for vault: {root}",
        "",
        "## Summary",
        "",
        f"- People: {len(people)}",
        f"- Companies: {len(companies)}",
        f"- Opportunities: {len(opportunities)}",
        f"- Relations: {len(relations)}",
        "",
        "## Likely Recruiters",
        "",
        *_line_items(recruiters),
        "",
        "## People By Current Company",
        "",
        *_line_items(company_lines),
        "",
        "## Companies With Known Contacts",
        "",
        *_line_items(known_contact_lines),
        "",
        "## Opportunities",
        "",
        *_line_items(opportunity_lines),
        "",
        "## Owner Goals And Targets",
        "",
        *_line_items(sorted(set(owner_goal_lines))),
        "",
        "## Owner Career History",
        "",
        *_line_items(owner_position_lines),
        "",
        "## Owner Skills And Certifications",
        "",
        *_line_items(sorted(set(owner_skill_lines))),
        "",
        "## Metadata Warnings",
        "",
        *_line_items(warnings),
        "",
        "## Orphan Career Entities",
        "",
        *_line_items(orphan_lines),
        "",
    ]
    return "\n".join(report)


career_report = generate_career_report
