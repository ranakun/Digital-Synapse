"""Validated owner knowledge and deterministic, read-only context assembly.

The profile opts existing Markdown entities into a small interpretation contract.
Evidence basis, owner position and lifecycle are independent of review_status.
No read in this module writes, reindexes, embeds, or generates new prose with a model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from synapse.models import Entity, Issue

KNOWLEDGE_PROFILE = "owner-knowledge-v1"
RECORD_KINDS = {
    "finding", "preference", "boundary", "question", "recommendation", "navigation", "evidence",
}
EPISTEMIC_BASES = {
    "owner-report", "interaction-observation", "artifact-evidence", "third-party-account",
    "assistant-hypothesis",
}
OWNER_POSITIONS = {"stated", "accepted", "disputed", "unreviewed", "not-applicable"}
LIFECYCLES = {"current", "historical", "withdrawn"}
KNOWLEDGE_ROLES = {"member", "about", "evidence", "qualifies", "revises", "applies_to"}
REQUIRED_SECTIONS = ("statement", "conditions and limits", "evidence")
_LABEL = re.compile(r"^[a-z][a-z0-9-]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCORE = re.compile(r"(?:^|_)(?:iq|intelligence|ability|certainty|confidence|percentile)(?:_|$)")
_MAX_PROFILE_RECORDS = 500
_MAX_REFERENCE_RECORDS = 2000
_UMBRELLAS = {
    "work": {
        "work", "reasoning", "engagement", "quality", "standards", "career", "interests",
        "support", "resources", "commitment", "collaboration",
    },
    "planning": {
        "planning", "engagement", "quality", "standards", "commitment", "collaboration",
        "career", "values",
    },
}


def _props(fm: dict[str, Any]) -> dict[str, Any]:
    value = fm.get("properties")
    return value if isinstance(value, dict) else {}


def _profiled(fm: dict[str, Any]) -> bool:
    return "knowledge_profile" in _props(fm)


def _strict_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid calendar date: {value}") from exc


def _today() -> date:
    return datetime.now(UTC).date()


def _budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("budget_chars must be a positive integer")
    return value


def _bounded_message(budget: int, *messages: str) -> str:
    for message in (*messages, "Omitted.", "…"):
        if len(message) <= budget:
            return message
    return ""


def _sections(body: str) -> tuple[dict[str, str], set[str]]:
    """Read authored level-two sections, ignoring headings inside fenced code."""
    sections: dict[str, list[str]] = {}
    duplicates: set[str] = set()
    current: str | None = None
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            char = marker.group(1)[0]
            if fence is None:
                fence = char
            elif fence == char:
                fence = None
            if current:
                sections[current].append(line)
            continue
        heading = None if fence else re.match(r"^(#{1,2})\s+(.+?)\s*#*\s*$", line)
        if heading:
            if len(heading.group(1)) == 1:
                current = None
                continue
            current = heading.group(2).strip().casefold()
            if current in sections:
                duplicates.add(current)
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}, duplicates


def validate_knowledge_record(frontmatter: dict, body: str) -> list[str]:
    """Validate an opted-in record. Unprofiled existing records are untouched.

    IDs are deliberately not validated here: projected create operations do not
    have a persisted ULID yet. Graph validation resolves dictionary keys instead.
    """
    if not _profiled(frontmatter):
        return []
    props = _props(frontmatter)
    errors: list[str] = []
    if props.get("knowledge_profile") != KNOWLEDGE_PROFILE:
        errors.append(f"knowledge_profile must be {KNOWLEDGE_PROFILE}")
    kind = props.get("record_kind")
    if kind not in RECORD_KINDS:
        errors.append("record_kind is not a supported owner-knowledge kind")
    allowed_types = {"insight", "conversation", "event"} if kind == "evidence" else {"insight"}
    if frontmatter.get("type") not in allowed_types:
        errors.append(f"record_kind {kind!r} requires type in {sorted(allowed_types)}")
    if props.get("subject_id") != "me":
        errors.append("subject_id must be the owner entity 'me'")
    for key in ("claim_key",):
        if not isinstance(props.get(key), str) or not props[key].strip():
            errors.append(f"{key} must be a nonempty string")
    if props.get("owner_position") not in OWNER_POSITIONS:
        errors.append("owner_position is not a supported position")
    if props.get("lifecycle") not in LIFECYCLES:
        errors.append("lifecycle must be current, historical, or withdrawn")
    bases = props.get("epistemic_basis")
    if not isinstance(bases, list) or not bases or any(
        not isinstance(item, str) or item not in EPISTEMIC_BASES for item in bases
    ):
        errors.append("epistemic_basis must be a nonempty list of supported source bases")
    facets = props.get("facets")
    if not isinstance(facets, list) or not facets or any(
        not isinstance(item, str) or not _LABEL.fullmatch(item) for item in facets
    ):
        errors.append("facets must be a nonempty list of lowercase kebab-case labels")
    dates: dict[str, date] = {}
    for key in ("as_of", "applies_from", "applies_until"):
        if key != "as_of" and key not in props:
            continue
        try:
            dates[key] = _strict_date(props.get(key), key)
        except ValueError as exc:
            errors.append(str(exc))
    if dates.get("applies_from", date.min) > dates.get("applies_until", date.max):
        errors.append("applies_from must not be later than applies_until")
    for key, value in props.items():
        flat = value is None or isinstance(value, (str, int, float, bool)) or (
            isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value)
        )
        if not flat:
            errors.append(f"property {key!r} must have a flat scalar or scalar-list value")
        numbers = value if isinstance(value, list) else [value]
        if _SCORE.search(str(key).lower()) and any(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in numbers
        ):
            errors.append(f"numeric ability/certainty score {key!r} is not part of this profile")
    if kind == "evidence" and not str(props.get("source_family_id") or "").strip():
        errors.append("evidence records require source_family_id")
    if "context_order" in props and (
        isinstance(props["context_order"], bool) or not isinstance(props["context_order"], int)
        or props["context_order"] < 1
    ):
        errors.append("context_order must be a positive integer presentation order")
    for key in ("source_family_ids", "source_refs"):
        if key in props and (
            not isinstance(props[key], list) or not props[key]
            or any(not isinstance(item, str) or not item.strip() for item in props[key])
        ):
            errors.append(f"{key} must be a nonempty list of nonempty strings")
    for key in ("source_family_id", "source_locator", "source_file"):
        if key in props and (not isinstance(props[key], str) or not props[key].strip()):
            errors.append(f"{key} must be a nonempty string when provided")
    sections, duplicates = _sections(body)
    for section in REQUIRED_SECTIONS:
        if not sections.get(section):
            errors.append(f"required Markdown section is absent or empty: {section}")
        if section in duplicates:
            errors.append(f"required Markdown section occurs more than once: {section}")
    relations = frontmatter.get("relations") or []
    if not isinstance(relations, list):
        errors.append("relations must be a list")
        return errors
    for i, relation in enumerate(relations):
        if not isinstance(relation, dict):
            errors.append(f"relation {i} must be a map")
            continue
        if relation.get("type") != "related_to":
            continue
        rp = relation.get("properties") or {}
        roles = rp.get("roles") if isinstance(rp, dict) else None
        if not isinstance(roles, list) or not roles or any(
            not isinstance(role, str) or role not in KNOWLEDGE_ROLES for role in roles
        ):
            errors.append(f"relation {i} roles must be a nonempty list of supported knowledge roles")
            continue
        if len(set(roles)) != len(roles):
            errors.append(f"relation {i} repeats a knowledge role")
        if not isinstance(rp.get("note"), str) or not rp["note"].strip():
            errors.append(f"relation {i} requires a readable note")
        if {"qualifies", "revises"} & set(roles) and (
            not isinstance(rp.get("scope"), str) or not rp["scope"].strip()
        ):
            errors.append(f"relation {i} qualifies/revises requires an explicit scope")
        if not isinstance(relation.get("target"), str) or not relation["target"].strip():
            errors.append(f"relation {i} requires a target reference")
        if relation.get("direction", "outgoing") not in {"outgoing", "incoming"}:
            errors.append(f"relation {i} has an invalid direction")
    return errors


def _entity_from_row(row: sqlite3.Row) -> Entity:
    fm = json.loads(row["frontmatter"] or "{}")
    props = _props(fm)
    return Entity(
        id=row["id"], type=row["type"], name=row["name"], file_path=Path(row["file_path"]),
        frontmatter=fm, body=row["body"] or "", content_hash=row["content_hash"],
        review_status=row["review_status"], aliases=fm.get("aliases") or [],
        tags=fm.get("tags") or [], properties=props, created_at=row["created_at"],
        updated_at=row["updated_at"], relation_specs=fm.get("relations") or [],
    )


def load_knowledge_entities(conn: sqlite3.Connection) -> dict[str, Entity]:
    """Load index rows for whole-graph validation, without changing the connection."""
    return {row["id"]: _entity_from_row(row) for row in conn.execute("SELECT * FROM entities ORDER BY id")}


def _semantic_edges(entities: dict[str, Entity]) -> list[tuple[str, str, dict[str, Any], str]]:
    edges = []
    for declaring_id, entity in sorted(entities.items()):
        for relation in entity.frontmatter.get("relations") or []:
            if not isinstance(relation, dict) or relation.get("type") != "related_to":
                continue
            rp = relation.get("properties") or {}
            if not isinstance(rp, dict):
                rp = {}
            rp = {**rp, "_declared_direction": relation.get("direction", "outgoing")}
            target = relation.get("target")
            if not isinstance(target, str):
                continue
            source, dest = declaring_id, target
            if relation.get("direction") == "incoming":
                source, dest = dest, source
            edges.append((source, dest, rp, declaring_id))
    return edges


def _roles(properties: dict[str, Any]) -> set[str]:
    value = properties.get("roles")
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def _graph_errors(entities: dict[str, Entity]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    profiled = {key: entity for key, entity in entities.items() if _profiled(entity.frontmatter)}
    for key, entity in sorted(profiled.items()):
        errors.extend((key, error) for error in validate_knowledge_record(entity.frontmatter, entity.body))
        owner = entities.get("me")
        if owner is None or owner.type != "person":
            errors.append((key, "subject 'me' must resolve to an indexed person"))
    edges = _semantic_edges(entities)
    seen: set[tuple[str, str]] = set()
    revisions: dict[str, set[str]] = defaultdict(set)
    members: set[str] = set()
    for source, target, rp, declaring in edges:
        if declaring not in profiled and source not in profiled and target not in profiled:
            continue
        issue_key = source if source in profiled else declaring
        roles = _roles(rp)
        raw_roles = rp.get("roles")
        if not isinstance(raw_roles, list) or not raw_roles or len(roles) != len(raw_roles) or roles - KNOWLEDGE_ROLES:
            errors.append((issue_key, f"knowledge edge {source} -> {target} requires a valid, nonrepeated roles list"))
        if rp.get("_declared_direction") not in {"incoming", "outgoing"}:
            errors.append((issue_key, f"knowledge edge {source} -> {target} has an invalid direction"))
        if not isinstance(rp.get("note"), str) or not rp["note"].strip():
            errors.append((issue_key, f"knowledge edge {source} -> {target} requires a readable note"))
        if {"qualifies", "revises"} & roles and (not isinstance(rp.get("scope"), str) or not rp["scope"].strip()):
            errors.append((issue_key, f"knowledge edge {source} -> {target} requires an explicit scope"))
        pair = (source, target)
        if pair in seen:
            errors.append((issue_key, f"duplicate related_to knowledge edge {source} -> {target}; combine roles"))
        seen.add(pair)
        if target not in entities or source not in entities:
            errors.append((issue_key, f"knowledge role target/source is unresolved: {source} -> {target}"))
            continue
        if source == target:
            errors.append((issue_key, "knowledge role must not refer to the same entity"))
        if "about" in roles and target != "me":
            errors.append((issue_key, "about role must point to subject 'me'"))
        if "member" in roles:
            target_props = _props(entities[target].frontmatter)
            if target not in profiled or target_props.get("record_kind") != "navigation":
                errors.append((issue_key, "member role must point to a profiled navigation record"))
            else:
                members.add(source)
        if "revises" in roles:
            revisions[source].add(target)
            if target in profiled and _props(entities[source].frontmatter).get("claim_key") != _props(entities[target].frontmatter).get("claim_key"):
                errors.append((issue_key, "revises must connect versions of the same claim_key; use qualifies for a narrower/different claim"))
    for key, entity in sorted(profiled.items()):
        if _props(entity.frontmatter).get("record_kind") != "navigation" and key not in members:
            errors.append((key, "profile record must belong to a navigation record through member"))
    for start in sorted(revisions):
        pending = list(revisions[start])
        visited: set[str] = set()
        while pending:
            node = pending.pop()
            if node == start:
                errors.append((start, "revision cycle detected"))
                break
            if node not in visited:
                visited.add(node)
                pending.extend(revisions.get(node, ()))
    current: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, entity in sorted(profiled.items()):
        props = _props(entity.frontmatter)
        if props.get("lifecycle") == "current" and isinstance(props.get("claim_key"), str):
            current[(str(props.get("subject_id")), props["claim_key"])].append((key, props))
    for versions in current.values():
        for i, (left_id, left) in enumerate(versions):
            for right_id, right in versions[i + 1:]:
                left_start, left_end = str(left.get("applies_from") or "0001"), str(left.get("applies_until") or "9999")
                right_start, right_end = str(right.get("applies_from") or "0001"), str(right.get("applies_until") or "9999")
                if max(left_start, right_start) <= min(left_end, right_end):
                    message = f"conflicting current versions of claim_key: {left_id}, {right_id}"
                    errors.extend([(left_id, message), (right_id, message)])
    return sorted(set(errors))


def validate_knowledge_graph(entities: dict[str, Entity]) -> list[Issue]:
    """Validate profile semantics and scoped graph links, including $new.* refs."""
    return [
        Issue("error", f"Owner knowledge {key}: {message}", entities[key].file_path if key in entities else None)
        for key, message in _graph_errors(entities)
    ]


def _fetch_ids(conn: sqlite3.Connection, ids: set[str]) -> dict[str, Entity]:
    result: dict[str, Entity] = {}
    ordered = sorted(ids)
    for offset in range(0, len(ordered), 400):
        chunk = ordered[offset:offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT * FROM entities WHERE id IN ({placeholders}) ORDER BY id", chunk)
        result.update((row["id"], _entity_from_row(row)) for row in rows)
    return result


def _snapshot(conn: sqlite3.Connection) -> tuple[dict[str, Entity], set[str], bool]:
    rows = conn.execute(
        "SELECT * FROM entities WHERE json_extract(frontmatter, '$.properties.knowledge_profile') IS NOT NULL ORDER BY id LIMIT ?",
        (_MAX_PROFILE_RECORDS + 1,),
    ).fetchall()
    truncated = len(rows) > _MAX_PROFILE_RECORDS
    profiles = {row["id"]: _entity_from_row(row) for row in rows[:_MAX_PROFILE_RECORDS]}
    targets = {"me"}
    for source, target, _rp, _declaring in _semantic_edges(profiles):
        targets.update((source, target))
    # Edges can be declared `incoming` on a legacy destination. Fetch those
    # destinations through the relation index too; their Markdown owns the spec.
    ids = sorted(profiles)
    for offset in range(0, len(ids), 300):
        chunk = ids[offset:offset + 300]
        placeholders = ",".join("?" for _ in chunk)
        targets.update(row[0] for row in conn.execute(
            f"SELECT DISTINCT to_id FROM relations WHERE type = 'related_to' AND from_id IN ({placeholders}) ORDER BY to_id LIMIT ?",
            (*chunk, _MAX_REFERENCE_RECORDS + 1),
        ))
    targets.difference_update(profiles)
    if len(targets) > _MAX_REFERENCE_RECORDS:
        truncated = True
        targets = set(sorted(targets)[:_MAX_REFERENCE_RECORDS]) | {"me"}
    entities = profiles | _fetch_ids(conn, targets)
    return entities, set(profiles), truncated


def _in_window(entity: Entity, at: date, *, historical: bool = False) -> bool:
    props = _props(entity.frontmatter)
    if props.get("lifecycle") == "withdrawn":
        return False
    if not historical and props.get("lifecycle") != "current":
        return False
    try:
        if _strict_date(props.get("as_of"), "as_of") > at:
            return False
        if props.get("applies_from") and _strict_date(props["applies_from"], "applies_from") > at:
            return False
        if props.get("applies_until") and _strict_date(props["applies_until"], "applies_until") < at:
            return False
    except ValueError:
        return False
    return True


def _incoming(
    entities: dict[str, Entity], entity_id: str, at: date, *, historical: bool = False,
) -> list[tuple[Entity, dict[str, Any]]]:
    result = []
    for source, target, rp, _declaring in _semantic_edges(entities):
        if target == entity_id and {"qualifies", "revises"}.intersection(_roles(rp)):
            record = entities.get(source)
            if record and _profiled(record.frontmatter):
                # Bad applicability metadata must not silently erase a known
                # incoming correction. Return an unvalidated alert instead.
                if _in_window(record, at, historical=historical) or validate_knowledge_record(record.frontmatter, record.body):
                    result.append((record, rp))
    return sorted(result, key=lambda item: (item[0].id, str(item[1].get("scope", ""))))


def _basis_line(entity: Entity) -> str:
    props = _props(entity.frontmatter)
    basis = props.get("epistemic_basis")
    basis_text = ", ".join(map(str, basis)) if isinstance(basis, list) else str(basis or "unspecified")
    line = (
        f"Basis: {basis_text}; owner: {props.get('owner_position', 'unspecified')}; "
        f"lifecycle: {props.get('lifecycle', 'unspecified')}; as of: {props.get('as_of', 'unspecified')}; "
        f"review: {entity.review_status}."
    )
    if props.get("source_family_id"):
        line += f" Source family: `{props['source_family_id']}`."
    elif props.get("source_family_ids"):
        line += " Source families: " + ", ".join(f"`{item}`" for item in props["source_family_ids"]) + "."
    for key in ("applies_from", "applies_until"):
        if props.get(key):
            line += f" {key}: {props[key]}."
    return line


def _core_card(entity: Entity) -> str:
    sections, _ = _sections(entity.body)
    return (
        f"### {entity.name} `{entity.id}`\n{_basis_line(entity)}\n\n"
        f"**Statement**\n{sections.get('statement', '')}\n\n"
        f"**Conditions and limits**\n{sections.get('conditions and limits', '')}"
    )


def _reference_lines(entities: dict[str, Entity], entity_id: str, *, compact: bool = False) -> str:
    lines = []
    for source, target, rp, _declaring in _semantic_edges(entities):
        if source != entity_id:
            continue
        roles = _roles(rp) - {"member", "about"}
        if not roles:
            continue
        peer = entities.get(target)
        label = peer.name if peer else "Unresolved record"
        line = f"- {', '.join(sorted(roles))}: {label} `{target}`"
        if rp.get("scope"):
            line += f"; scope: {rp['scope']}"
        if rp.get("note") and not compact:
            line += f" — {rp['note']}"
        if rp.get("source_family_id"):
            line += f"; source family: `{rp['source_family_id']}`"
        elif rp.get("source_family_ids"):
            line += "; source families: " + ", ".join(f"`{item}`" for item in rp["source_family_ids"])
        if rp.get("source_locator") and not compact:
            line += f"; locator: {rp['source_locator']}"
        if peer and _props(peer.frontmatter).get("source_family_id") and not rp.get("source_family_id"):
            line += f"; source family: `{_props(peer.frontmatter)['source_family_id']}`"
        lines.append(line)
    return "\n".join(sorted(set(lines)))


def _card_bundle(
    entities: dict[str, Entity], entity: Entity, at: date, *, historical: bool = False,
    invalid_ids: set[str] | None = None, compact: bool = False,
) -> str:
    parts = [_core_card(entity)]
    references = _reference_lines(entities, entity.id, compact=compact)
    if references:
        parts.append("**Linked evidence/context**\n" + references)
    # Include incoming qualifications' authored essentials with the claim.
    # A later formatter may omit this whole bundle; it must not trim this tail.
    seen = {entity.id}
    pending = [entity.id]
    while pending:
        target = pending.pop(0)
        for qualifier, rp in _incoming(entities, target, at, historical=historical):
            if qualifier.id in seen:
                continue
            seen.add(qualifier.id)
            if qualifier.id in (invalid_ids or set()) or validate_knowledge_record(qualifier.frontmatter, qualifier.body):
                parts.append(f"**Unvalidated correction** `{qualifier.id}` affects `{target}`; inspect before relying on the claim.")
                continue
            roles = ", ".join(sorted(_roles(rp) & {"qualifies", "revises"}))
            parts.append(f"**{roles} `{target}` — scope: {rp.get('scope', 'unspecified')}**\n" + _core_card(qualifier))
            pending.append(qualifier.id)
    return "\n\n".join(parts)


def _notice(
    entities: dict[str, Entity], entity_id: str, at: date, budget_chars: int,
) -> str:
    entity = entities.get(entity_id)
    if entity is None:
        return ""
    incoming = _incoming(entities, entity_id, at)
    profiled = _profiled(entity.frontmatter)
    if not profiled and not incoming:
        return ""
    alerts = []
    invalid_ids = {key for key, _message in _graph_errors(entities)}
    for qualifier, rp in incoming:
        roles = ", ".join(sorted(_roles(rp) & {"qualifies", "revises"}))
        prefix = "unvalidated " if qualifier.id in invalid_ids else ""
        alerts.append(f"{prefix}{roles}: `{qualifier.id}` (scope: {rp.get('scope', 'unspecified')})")
    warning = ""
    if alerts:
        warning = "**Qualified/revised record: do not rely on it alone.** " + "; ".join(alerts) + "."
    metadata = "Owner knowledge. " + _basis_line(entity) if profiled else ""
    if profiled:
        metadata += " Read the statement with its conditions and limits; attestation does not establish a hypothesis."
        if entity_id in invalid_ids:
            metadata = "**Unvalidated owner-knowledge record.** " + metadata
    base = "\n".join(part for part in (warning, metadata) if part)
    if len(base) <= budget_chars:
        for qualifier, _rp in incoming:
            if qualifier.id not in invalid_ids:
                addition = "\n\n" + _core_card(qualifier)
                if len(base) + len(addition) <= budget_chars:
                    base += addition
        return base
    if alerts:
        ids = ", ".join(f"`{qualifier.id}`" for qualifier, _rp in incoming)
        return _bounded_message(
            budget_chars,
            f"Qualified/revised; read corrections before use: {ids}. Full scopes/limits omitted; use owner-context or a larger brief budget.",
            f"Qualified/revised by {ids}; read corrections before use.",
            "Qualified/revised. Correction IDs and scope do not fit; raise the budget before relying on this record.",
            "Qualified/revised; raise budget before use.",
        )
    return _bounded_message(
        budget_chars, metadata,
        f"Owner knowledge: {_props(entity.frontmatter).get('lifecycle', 'unspecified')}; basis {', '.join(map(str, _props(entity.frontmatter).get('epistemic_basis') or []))}. Read conditions/limits before use.",
        "Owner knowledge; basis and conditions omitted. Raise budget before use.",
    )


def format_knowledge_notice(
    conn: sqlite3.Connection, entity_id: str, *, budget_chars: int = 1500,
) -> str:
    """Basis/lifecycle and incoming scoped corrections, including legacy targets."""
    budget_chars = _budget(budget_chars)
    entities, _profiles, truncated = _snapshot(conn)
    if entity_id not in entities:
        entities.update(_fetch_ids(conn, {entity_id}))
    text = _notice(entities, entity_id, _today(), budget_chars)
    if truncated:
        return _bounded_message(budget_chars, "Owner-knowledge lookup exceeded its safety cap; corrections may be omitted. Refine the stored collection before relying on this record.")
    return text


def format_profile_entity(
    conn: sqlite3.Connection, entity_id: str, *, budget_chars: int = 6000,
) -> str | None:
    """Render a marked entity faithfully; return None for ordinary legacy entities."""
    budget_chars = _budget(budget_chars)
    entities, profiles, truncated = _snapshot(conn)
    if entity_id not in profiles:
        row = _fetch_ids(conn, {entity_id}).get(entity_id)
        if row is None or not _profiled(row.frontmatter):
            return None
        entities[entity_id] = row
    entity = entities[entity_id]
    if truncated:
        return _bounded_message(budget_chars, f"Owner knowledge `{entity_id}` omitted: collection exceeds the read cap; full corrections cannot be guaranteed.")
    invalid = {key for key, _message in _graph_errors(entities)}
    if entity_id in invalid:
        return _bounded_message(budget_chars, f"Owner knowledge `{entity_id}` is structurally unvalidated; no claim rendered. Run check and inspect the source.")
    core = _card_bundle(entities, entity, _today(), invalid_ids=invalid)
    if len(core) > budget_chars:
        return _bounded_message(
            budget_chars,
            f"Owner knowledge `{entity_id}` omitted: statement, limits, and corrections must be read together. Raise budget_chars.",
            "Statement and limits omitted together; raise budget_chars.",
        )
    sections, _ = _sections(entity.body)
    evidence = "\n\n**Evidence**\n" + sections["evidence"]
    if len(core) + len(evidence) <= budget_chars:
        core += evidence
    else:
        tail = "\n\nEvidence detail omitted; raise budget_chars."
        if len(core) + len(tail) <= budget_chars:
            core += tail
    return core


def owner_context_pointer(conn: sqlite3.Connection) -> str:
    """Compact navigation without expanding the owner hub or reading its body."""
    entities, profiles, truncated = _snapshot(conn)
    nav = sorted(
        (entities[key] for key in profiles if _props(entities[key].frontmatter).get("record_kind") == "navigation" and _in_window(entities[key], _today())),
        key=lambda entity: entity.id,
    )
    if not nav:
        return ""
    ids = ", ".join(f"`{entity.id}`" for entity in nav[:3])
    suffix = " More navigation records omitted." if len(nav) > 3 or truncated else ""
    return f"Owner knowledge: {ids}. Use `synapse owner-context` / `synapse_owner_context` for relevant findings, conditions, evidence, and corrections.{suffix}"


def _legacy_card(entity: Entity, note: str, notice: str) -> str:
    props = _props(entity.frontmatter)
    lines = [f"### Linked context: {entity.name} `{entity.id}`", f"Type: {entity.type}; review: {entity.review_status}."]
    for key in ("as_of", "applies_from", "applies_until", "evidence_status", "schedule_status"):
        if props.get(key):
            lines.append(f"{key}: {props[key]}")
    if note:
        lines.append("Recorded connection: " + note)
    if notice:
        lines.append(notice)
    lines.append("Reference only: retrieve its brief for the authored context; its full body is not reproduced here.")
    return "\n".join(lines)


def build_owner_context(
    conn: sqlite3.Connection, *, facet: str | None = None, target_id: str | None = None,
    as_of: str | None = None, budget_chars: int = 8000,
) -> str:
    """Assemble scoped, dated owner knowledge from indexed authored records only.

    Invalid query arguments raise ValueError. Positive tiny budgets receive an
    omission notice, never a partial assertion. Historical records are eligible
    only for an explicit as_of request; an applicable explicit revision still wins.
    """
    budget_chars = _budget(budget_chars)
    at = _strict_date(as_of, "as_of") if as_of is not None else _today()
    historical = as_of is not None
    entities, profiles, truncated = _snapshot(conn)
    if target_id is not None:
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("target_id must be an existing entity ID")
        entities.update(_fetch_ids(conn, {target_id}))
        if target_id not in entities:
            raise ValueError(f"Unknown target entity ID: {target_id}")
    available = sorted({value for key in profiles for value in _props(entities[key].frontmatter).get("facets", []) if isinstance(value, str)})
    if facet is not None:
        if not isinstance(facet, str) or facet not in set(available) | set(_UMBRELLAS):
            raise ValueError(f"Unknown facet {facet!r}. Available: {', '.join(sorted(set(available) | set(_UMBRELLAS)))}")
    if truncated:
        return _bounded_message(budget_chars, "Owner-knowledge collection exceeds the bounded read cap; no claims rendered because qualifications may be missing.")
    if not profiles:
        return _bounded_message(budget_chars, "No integrated owner-knowledge records are indexed. Existing personal context may still be available through search and brief.")
    graph_errors = _graph_errors(entities)
    invalid = {key for key, _message in graph_errors}
    nav = {key for key in profiles if key not in invalid and _props(entities[key].frontmatter).get("record_kind") == "navigation" and _in_window(entities[key], at, historical=historical)}
    if not nav:
        return _bounded_message(budget_chars, "No valid owner-knowledge navigation record applies on this date. No profile claims rendered; run check or retrieve historical sources.")
    edges = _semantic_edges(entities)
    members = {source for source, target, rp, _declaring in edges if target in nav and "member" in _roles(rp)}
    live = {
        key for key in profiles & members if key not in invalid and _in_window(entities[key], at, historical=historical)
    }
    superseded = {target for source, target, rp, _declaring in edges if source in live and "revises" in _roles(rp)}
    live -= superseded
    wanted = _UMBRELLAS.get(facet, {facet}) if facet else set()
    target_matches = {source for source, target, rp, _declaring in edges if target == target_id and "applies_to" in _roles(rp)} if target_id else set()
    core_matches = {key for key in live if _props(entities[key].frontmatter).get("record_kind") in {"preference", "boundary"} and "collaboration" in _props(entities[key].frontmatter).get("facets", [])}
    primary = []
    for key in live:
        props = _props(entities[key].frontmatter)
        if props.get("record_kind") in {"navigation", "evidence"}:
            continue
        matches_facet = bool(wanted.intersection(props.get("facets", [])))
        if not facet and not target_id or key in core_matches or key in target_matches or matches_facet:
            primary.append(key)
    primary.sort(key=lambda key: (
        key not in target_matches,
        facet not in _props(entities[key].frontmatter).get("facets", []) if facet and facet not in _UMBRELLAS else False,
        not bool(wanted.intersection(_props(entities[key].frontmatter).get("facets", []))) if facet else False,
        _props(entities[key].frontmatter).get("context_order", 1000),
        str(_props(entities[key].frontmatter).get("claim_key")), key,
    ))
    header = (
        f"# Owner context `me`\nAs of: {at.isoformat()}; facet: {facet or 'overview'}"
        + (f"; target: `{target_id}`" if target_id else "")
        + ".\nAuthored knowledge, not a diagnosis or an instruction to act. Review status is separate from source basis and owner adoption. Repeated records in one source family are not independent corroboration."
    )
    topics = sorted(set(available) | set(_UMBRELLAS))
    shown_topics: list[str] = []
    for topic in topics:
        if len(", ".join([*shown_topics, topic])) > 400:
            break
        shown_topics.append(topic)
    header += "\nAvailable facets: " + ", ".join(shown_topics)
    if len(shown_topics) < len(topics):
        header += f" ({len(topics) - len(shown_topics)} more omitted)"
    header += "."
    if invalid:
        header += f"\n{len(invalid)} structurally invalid record(s) excluded; run check before relying on affected claims."
    if target_id and not target_matches:
        header += "\nNo focal finding has a recorded applies_to link to this target; any shared context below is general."
    if not primary:
        return _bounded_message(budget_chars, header + "\nNo current focal findings match this selection.")
    cards: list[tuple[str, str]] = []
    selected = set(primary)
    for key in primary:
        cards.append((key, _card_bundle(entities, entities[key], at, historical=historical, invalid_ids=invalid, compact=True)))
    # Source episodes and relevant legacy constraints are references/supporting
    # cards only. There is deliberately no traversal outward from the owner hub.
    supporting: dict[str, list[str]] = defaultdict(list)
    for source, target, rp, _declaring in edges:
        if source in selected and {"evidence", "applies_to"}.intersection(_roles(rp)) and target not in selected and target != "me":
            supporting[target].append(str(rp.get("note") or ""))
    for key, notes in sorted(supporting.items()):
        record = entities.get(key)
        if record is None:
            continue
        if key in profiles and key not in invalid and _in_window(record, at, historical=True):
            # Historical source episodes remain evidence, clearly dated; they do
            # not become current personal instructions through this inclusion.
            cards.append((key, _card_bundle(entities, record, at, historical=historical, invalid_ids=invalid, compact=True)))
        elif key not in profiles:
            cards.append((key, _legacy_card(record, " ".join(sorted(set(notes))), _notice(entities, key, at, 1500))))
    omitted: list[str] = []
    kept: list[str] = []
    included_ids: set[str] = set()
    # Reserve enough room to announce omissions even when every full card fits
    # poorly. A tiny budget produces only an omission notice.
    reserve = min(220, max(40, budget_chars // 6))
    used = len(header)
    for key, card in cards:
        if key in included_ids:
            continue  # Already reproduced as a complete incoming correction.
        if used + 2 + len(card) <= budget_chars - reserve:
            kept.append(card)
            used += 2 + len(card)
            included_ids.add(key)
            included_ids.update(re.findall(r"^### .+ `([0-9A-HJKMNP-TV-Z]{26})`$", card, re.MULTILINE))
        else:
            omitted.append(key)
    if not kept:
        return _bounded_message(
            budget_chars,
            f"Owner context: {len(cards)} record(s) omitted; statement, limits and corrections do not fit together. Raise budget_chars or select a narrower facet/target.",
            "Owner context omitted; raise budget_chars or narrow the selection.",
        )
    output = header + "\n\n" + "\n\n".join(kept)
    if omitted:
        tail = f"\n\nOmitted {len(omitted)} complete record(s); refine selection or raise budget_chars."
        id_tail = " IDs: " + ", ".join(f"`{key}`" for key in omitted) + "."
        if len(output) + len(tail) + len(id_tail) <= budget_chars:
            tail += id_tail
        if len(output) + len(tail) <= budget_chars:
            output += tail
    return output
