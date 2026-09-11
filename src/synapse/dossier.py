"""Deterministic company/person dossier assembly.

A dossier is a single, budgeted, read-time join over the existing graph — no
new materialized edges are ever written (ARCHITECTURE SS1 forbids colleague
edges). It reuses `brief.py`'s row/relation helpers and its
priority-ordered, lowest-priority-first budget trimming (`_trim_to_budget`).

Same input -> same bytes. No LLM prose anywhere in this module.
"""

from __future__ import annotations

from typing import Any

try:
    import pysqlite3 as sqlite3
except Exception:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

from synapse.brief import (
    _PROP_SKIP,
    _get_row,
    _rel_query,
    _row_fm,
    _row_props,
    _trim_to_budget,
    _uv,
    _window,
    _windows_overlap,
    relation_evidence_text,
)
from synapse.warmpath import outreach_exclusion_sources, rank_connectors

DOSSIER_HEADER = (
    "<!-- Digital Synapse dossier — derived from the index; "
    "regenerate with `synapse dossier <ref>` -->\n"
    "> **Facts marked (unverified) are machine-imported and unreviewed.**\n"
)

DEFAULT_DOSSIER_BUDGET_TOKENS = 12000


def _rel_key(rel: dict[str, Any]) -> tuple[str, str, str, str]:
    """Stable identity for a single relation instance (not just its type).

    Used to track exactly which relation *rows* earlier dossier sections
    rendered, so the "everything else" section can exclude only those specific
    edges instead of blanket-excluding every relation of a given type (which
    previously dropped e.g. an `introduced_by` edge pointing at someone other
    than `me` — see FIX-20).
    """
    return (rel["from_id"], rel["to_id"], rel["type"], str(rel.get("created_at") or ""))


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _status_of(conn: sqlite3.Connection, entity_id: str) -> str:
    row = _get_row(conn, entity_id)
    return row["review_status"] if row else "proposed"


def _uv_any(*statuses: str) -> str:
    """Label (unverified) if any of the given review_status values is 'proposed'."""
    return " (unverified)" if "proposed" in statuses else ""


def _rel_between(
    conn: sqlite3.Connection, a_id: str, b_id: str, rel_type: str, *, include_weak: bool = False
) -> list[dict[str, Any]]:
    """Relations of rel_type strictly between a_id and b_id, either direction.

    Real-vault edge direction for some relation types (e.g. `participated_in`)
    is not perfectly consistent between hand-authored and imported data, so
    "how are these two entities connected" evidence is gathered undirected.
    """
    out = _rel_query(conn, from_id=a_id, to_id=b_id, rel_type=rel_type, include_weak=include_weak)
    out += _rel_query(conn, from_id=b_id, to_id=a_id, rel_type=rel_type, include_weak=include_weak)
    return out


def _other_end(rel: dict[str, Any], anchor_id: str) -> tuple[str, str]:
    """Return (peer_id, peer_name) — the end of rel that is not anchor_id."""
    if rel["from_id"] == anchor_id:
        return rel["to_id"], rel["to_name"]
    return rel["from_id"], rel["from_name"]


# ---------------------------------------------------------------------------
# COMPANY dossier
# ---------------------------------------------------------------------------

def _c1_identity(row: sqlite3.Row) -> str:
    props = _row_props(row)
    fm = _row_fm(row)
    uv = _uv(row["review_status"])
    lines = ["## 1. Identity\n"]
    lines.append(f"**{row['name']}**{uv} `{row['id']}`")
    lines.append(f"Type: {row['type']} | Status: {row['review_status']}")
    aliases = fm.get("aliases") or []
    if aliases:
        lines.append(f"Aliases: {', '.join(str(a) for a in aliases)}")
    domain = props.get("domain") or ""
    industry = props.get("industry") or ""
    if domain:
        lines.append(f"Domain: {domain}")
    if industry:
        lines.append(f"Industry: {industry}")
    return "\n".join(lines)


def _contact_line(conn: sqlite3.Connection, rel: dict[str, Any], *, window_suffix: bool) -> str:
    person_id = rel["from_id"]
    person_row = _get_row(conn, person_id)
    person_status = person_row["review_status"] if person_row else "proposed"
    props = rel["properties"] if isinstance(rel["properties"], dict) else {}
    role = props.get("role") or ""
    if not role and person_row:
        role = _row_props(person_row).get("current_role") or ""
    role_str = f" — {role}" if role else ""
    period_str = ""
    if window_suffix:
        start, end = _window(props)
        if start:
            period_str = f" ({start} — {end or '?'})"
    uv = _uv_any(person_status, rel["review_status"])
    owner_relations: list[str] = []
    for rel_type in ("has_interaction", "collaborates_with", "introduced_by"):
        direct = _rel_between(conn, "me", person_id, rel_type)
        if direct:
            evidence = relation_evidence_text(direct[0])
            owner_relations.append(
                f"`{rel_type}`" + (f" ({evidence})" if evidence else "")
            )
    annotations: list[str] = []
    if owner_relations:
        annotations.append("direct owner relationship: " + ", ".join(owner_relations))
    exclusion_sources = outreach_exclusion_sources(conn).get(person_id, [])
    if exclusion_sources:
        cited = ", ".join(f"`{source_id}`" for source_id in exclusion_sources)
        annotations.append(f"excluded from job-search outreach by {cited}")
    annotation_str = f" — {'; '.join(annotations)}" if annotations else ""
    return f"- {rel['from_name']}{uv} `{person_id}`{role_str}{period_str}{annotation_str}"


def _c2_current_contacts(conn: sqlite3.Connection, current_rels: list[dict[str, Any]]) -> str:
    lines = ["## 2. Current Contacts\n"]
    if not current_rels:
        lines.append("(none found)")
        return "\n".join(lines)
    for rel in sorted(current_rels, key=lambda r: r["from_name"]):
        lines.append(_contact_line(conn, rel, window_suffix=False))
    return "\n".join(lines)


def _c3_former_contacts(conn: sqlite3.Connection, former_rels: list[dict[str, Any]]) -> str:
    lines = ["## 3. Former Contacts\n"]
    if not former_rels:
        lines.append("(none found)")
        return "\n".join(lines)
    for rel in sorted(former_rels, key=lambda r: r["from_name"]):
        lines.append(_contact_line(conn, rel, window_suffix=True))
    return "\n".join(lines)


def _c4_owner_overlap(
    conn: sqlite3.Connection,
    company_id: str,
    current_rels: list[dict[str, Any]],
    former_rels: list[dict[str, Any]],
) -> str:
    lines = ["## 4. Owner's Own History & Overlap\n"]
    owner_current = _rel_query(conn, from_id="me", to_id=company_id, rel_type="works_at")
    owner_former = _rel_query(conn, from_id="me", to_id=company_id, rel_type="former_employee_of")
    owner_rels = owner_current + owner_former
    if not owner_rels:
        lines.append("Owner has no recorded `works_at`/`former_employee_of` edge to this company.")
    else:
        for rel in owner_rels:
            props = rel["properties"] if isinstance(rel["properties"], dict) else {}
            start, end = _window(props)
            period = f" ({start} — {end or 'present'})" if start else ""
            kind = "current employee" if rel["type"] == "works_at" else "former employee"
            lines.append(f"- Owner is a {kind}{period}")

    owner_windows = [
        _window(r["properties"] if isinstance(r["properties"], dict) else {}) for r in owner_rels
    ]
    overlap_lines: list[str] = []
    if owner_windows:
        for rel in current_rels + former_rels:
            props = rel["properties"] if isinstance(rel["properties"], dict) else {}
            contact_window = _window(props)
            if any(_windows_overlap(ow, contact_window) for ow in owner_windows):
                start, end = contact_window
                period = f" ({start} — {end or 'present'})" if start else ""
                person_status = _status_of(conn, rel["from_id"])
                uv = _uv_any(person_status, rel["review_status"])
                overlap_lines.append(f"- {rel['from_name']}{uv} `{rel['from_id']}`{period}")
    if overlap_lines:
        lines.append("\n**Contacts whose tenure overlaps the owner's:**")
        lines.extend(overlap_lines)
    return "\n".join(lines)


def _c5_activity(conn: sqlite3.Connection, company_id: str) -> str:
    lines = ["## 5. Conversations, Opportunities & Events\n"]

    incoming = _rel_query(conn, to_id=company_id)
    opp_lines: list[str] = []
    recruiter_lines: list[str] = []
    for rel in incoming:
        from_row = _get_row(conn, rel["from_id"])
        if not from_row:
            continue
        uv = _uv_any(from_row["review_status"], rel["review_status"])
        if rel["type"] == "targets" and from_row["type"] == "opportunity":
            opp_lines.append(f"- {rel['from_name']}{uv} `{rel['from_id']}` (`targets`)")
        elif rel["type"] == "recruits_for" and from_row["type"] == "person":
            recruiter_lines.append(f"- {rel['from_name']}{uv} `{rel['from_id']}` (`recruits_for`)")

    weak_incoming = _rel_query(conn, to_id=company_id, rel_type="mentioned_in", include_weak=True)
    conv_lines: list[str] = []
    event_lines: list[str] = []
    for rel in weak_incoming:
        from_row = _get_row(conn, rel["from_id"])
        if not from_row:
            continue
        uv = _uv_any(from_row["review_status"], rel["review_status"])
        if from_row["type"] == "conversation":
            conv_lines.append(f"- {rel['from_name']}{uv} `{rel['from_id']}`")
        elif from_row["type"] == "event":
            event_lines.append(f"- {rel['from_name']}{uv} `{rel['from_id']}`")

    related_lines: list[str] = []
    related_rels = _rel_query(
        conn, to_id=company_id, rel_type="related_to", include_weak=True
    )
    related_rels += _rel_query(
        conn, from_id=company_id, rel_type="related_to", include_weak=True
    )
    seen_related: set[str] = set()
    for rel in related_rels:
        related_id, related_name = _other_end(rel, company_id)
        if related_id in seen_related:
            continue
        related_row = _get_row(conn, related_id)
        if not related_row or related_row["type"] not in ("insight", "project"):
            continue
        seen_related.add(related_id)
        uv = _uv_any(related_row["review_status"], rel["review_status"])
        related_lines.append(
            f"- {related_name}{uv} `{related_id}` ({related_row['type']})"
        )

    any_content = False
    if opp_lines:
        any_content = True
        lines.append("**Opportunities targeting this company**")
        lines.extend(sorted(set(opp_lines)))
    if recruiter_lines:
        any_content = True
        lines.append("\n**Recruiters recruiting for this company**")
        lines.extend(sorted(set(recruiter_lines)))
    if conv_lines:
        any_content = True
        lines.append("\n**Conversations mentioning this company**")
        lines.extend(sorted(set(conv_lines)))
    if event_lines:
        any_content = True
        lines.append("\n**Events mentioning this company**")
        lines.extend(sorted(set(event_lines)))
    if related_lines:
        any_content = True
        lines.append("\n**Related projects and insights**")
        lines.extend(sorted(related_lines))
    if not any_content:
        lines.append("(none found)")
    return "\n".join(lines)


def _c6_warm_entry_points(conn: sqlite3.Connection, company_id: str) -> str:
    lines = ["## 6. Recommended Warm Entry Points\n"]
    lines.append(
        "_Evidence-ranked connectors (G3) — additive, hand-set weights over "
        "existing signals: interaction depth/recency, employment at the target, "
        "shared events, introductions, endorsements, geography. Read-only; no "
        "new edges._"
    )
    ranked = rank_connectors(conn, company_id, limit=10)
    if not ranked:
        lines.append("\n(no ranked connectors found)")
        return "\n".join(lines)

    lines.append("")
    for cand in ranked:
        uv = _uv(cand["review_status"])
        evidence = "; ".join(cand["evidence"])
        lines.append(f"- {cand['name']}{uv} `{cand['id']}` — score {cand['score']} — {evidence}")
    return "\n".join(lines)


def build_company_dossier(
    conn: sqlite3.Connection,
    entity_id: str,
    budget_tokens: int = DEFAULT_DOSSIER_BUDGET_TOKENS,
) -> str:
    """Assemble a company dossier: identity, current/former contacts, owner
    overlap, touching conversations/opportunities/events, and a warm-entry
    stub. Read-time joins only — no new edges are written.
    """
    row = _get_row(conn, entity_id)
    if not row:
        return f"<!-- entity '{entity_id}' not found in index -->\n"
    if row["type"] != "company":
        return (
            f"<!-- entity '{entity_id}' ({row['name']}) is type={row['type']}, not company; "
            "use build_person_dossier or `synapse brief` -->\n"
        )

    current_rels = _rel_query(conn, to_id=entity_id, rel_type="works_at")
    former_rels = _rel_query(conn, to_id=entity_id, rel_type="former_employee_of")

    sections: list[tuple[int, str, str]] = [
        (1, "Identity", _c1_identity(row)),
        (2, "Current Contacts", _c2_current_contacts(conn, current_rels)),
        (3, "Former Contacts", _c3_former_contacts(conn, former_rels)),
        (4, "Owner History & Overlap", _c4_owner_overlap(conn, entity_id, current_rels, former_rels)),
        (5, "Activity", _c5_activity(conn, entity_id)),
        (6, "Warm Entry Points", _c6_warm_entry_points(conn, entity_id)),
    ]

    kept, trimmed = _trim_to_budget(sections, budget_tokens)
    parts = [DOSSIER_HEADER]
    for _, _, text in kept:
        parts.append(text)
    if trimmed:
        parts.append(f"<!-- truncated: {', '.join(trimmed)} -->")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# PERSON dossier
# ---------------------------------------------------------------------------

def _p1_identity(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    props = _row_props(row)
    uv = _uv(row["review_status"])
    lines = ["## 1. Identity & Role\n"]
    lines.append(f"**{row['name']}**{uv} `{row['id']}`")
    lines.append(f"Type: {row['type']} | Status: {row['review_status']}")

    company = props.get("current_company") or ""
    role = props.get("current_role") or props.get("role") or ""
    if not company or not role:
        works = _rel_query(conn, from_id=row["id"], rel_type="works_at")
        if works:
            company = company or works[0]["to_name"]
            wprops = works[0]["properties"] if isinstance(works[0]["properties"], dict) else {}
            role = role or wprops.get("role") or ""
    if role and company:
        lines.append(f"{role} @ {company}")
    elif role:
        lines.append(role)
    elif company:
        lines.append(f"@ {company}")

    location = props.get("location") or ""
    if location:
        lines.append(f"Location: {location}")
    return "\n".join(lines)


def _p2_relationship_evidence(
    conn: sqlite3.Connection, entity_id: str, consumed: set[tuple[str, str, str, str]]
) -> str:
    lines = ["## 2. Relationship Evidence\n"]
    parts: list[str] = []

    connection_lines: list[str] = []
    for rtype in ("knows", "introduced_by", "collaborates_with", "met_at"):
        for rel in _rel_between(conn, entity_id, "me", rtype):
            peer_id, _peer_name = _other_end(rel, entity_id)
            direction = "→" if rel["from_id"] == entity_id else "←"
            evidence = relation_evidence_text(rel)
            evidence_str = f" — {evidence}" if evidence else ""
            connection_lines.append(
                f"- {direction} `{rel['type']}` (peer: `{peer_id}`){evidence_str}"
            )
            consumed.add(_rel_key(rel))
    if connection_lines:
        parts.append("**Direct connection to owner**\n" + "\n".join(sorted(set(connection_lines))))

    owner_current = _rel_query(conn, from_id="me", rel_type="works_at")
    owner_former = _rel_query(conn, from_id="me", rel_type="former_employee_of")
    owner_employers = {r["to_id"] for r in owner_current}
    owner_employers |= {r["to_id"] for r in owner_former}
    person_current = _rel_query(conn, from_id=entity_id, rel_type="works_at")
    person_former = _rel_query(conn, from_id=entity_id, rel_type="former_employee_of")
    person_employers = {r["to_id"] for r in person_current}
    person_employers |= {r["to_id"] for r in person_former}
    shared_employers = owner_employers & person_employers
    if shared_employers:
        emp_lines = []
        for cid in sorted(shared_employers):
            crow = _get_row(conn, cid)
            if crow:
                emp_lines.append(f"- {crow['name']}{_uv(crow['review_status'])} `{cid}`")
        parts.append("**Shared employers**\n" + "\n".join(emp_lines))
        # The specific person-side works_at/former_employee_of edges that
        # established the overlap are rendered here (as the company name);
        # don't also repeat them verbatim in §4.
        for rel in person_current + person_former:
            if rel["to_id"] in shared_employers:
                consumed.add(_rel_key(rel))

    owner_attended = {r["to_id"] for r in _rel_query(conn, from_id="me", rel_type="attended")}
    person_attended_rels = _rel_query(conn, from_id=entity_id, rel_type="attended")
    person_met_at_rels = _rel_query(conn, from_id=entity_id, rel_type="met_at")
    person_attended = {r["to_id"] for r in person_attended_rels}
    person_met_at = {r["to_id"] for r in person_met_at_rels}
    shared_venues = owner_attended & (person_attended | person_met_at)
    if shared_venues:
        venue_lines = []
        for vid in sorted(shared_venues):
            vrow = _get_row(conn, vid)
            if vrow:
                label = "school" if vrow["type"] not in ("event",) else "event"
                venue_lines.append(f"- {vrow['name']}{_uv(vrow['review_status'])} `{vid}` ({label})")
        parts.append("**Shared schools / events**\n" + "\n".join(venue_lines))
        for rel in person_attended_rels + person_met_at_rels:
            if rel["to_id"] in shared_venues:
                consumed.add(_rel_key(rel))

    if not parts:
        parts.append("(no direct connection or shared employer/school/event context found)")
    return "\n".join([lines[0]] + parts)


def _p3_conversation_history(
    conn: sqlite3.Connection, entity_id: str, consumed: set[tuple[str, str, str, str]]
) -> str:
    lines = ["## 3. Conversation History\n"]
    has_int = _rel_query(conn, from_id="me", to_id=entity_id, rel_type="has_interaction")
    if has_int:
        lines.append(
            "`has_interaction` on record: the owner has had a substantive call or meeting "
            "with this person without a recorded transcript.\n"
        )
        for rel in has_int:
            evidence = relation_evidence_text(rel)
            if evidence:
                lines.append(f"- {evidence}")
            consumed.add(_rel_key(rel))

    rels = _rel_query(conn, from_id=entity_id, rel_type="participated_in")
    rels += _rel_query(conn, to_id=entity_id, rel_type="participated_in")
    seen: set[str] = set()
    conv_rows: list[tuple[str, sqlite3.Row, dict[str, Any]]] = []
    for rel in rels:
        conv_id, _ = _other_end(rel, entity_id)
        if conv_id in seen:
            continue
        conv_row = _get_row(conn, conv_id)
        if not conv_row or conv_row["type"] != "conversation":
            continue
        seen.add(conv_id)
        conv_rows.append((conv_id, conv_row, rel))
        consumed.add(_rel_key(rel))

    if not conv_rows and not has_int:
        lines.append("(no conversation history found)")
        return "\n".join(lines)
    if not conv_rows:
        return "\n".join(lines)

    def _date_key(item: tuple[str, sqlite3.Row, dict[str, Any]]) -> str:
        _, conv_row, _ = item
        props = _row_props(conv_row)
        date = (
            props.get("last_message_at")
            or props.get("date")
            or (conv_row["created_at"] or "")
        )
        return str(date)

    conv_rows.sort(key=_date_key, reverse=True)
    for conv_id, conv_row, rel in conv_rows:
        props = _row_props(conv_row)
        date = (
            props.get("last_message_at")
            or props.get("date")
            or (conv_row["created_at"] or "")
        )
        date = str(date)[:10]
        channel = props.get("channel") or ""
        channel_str = f" [{channel}]" if channel else ""
        uv = _uv_any(conv_row["review_status"], rel["review_status"])
        lines.append(f"- {conv_row['name']}{uv} `{conv_id}`{channel_str} ({date})")
    return "\n".join(lines)


def _p4_everything_else(
    conn: sqlite3.Connection, row: sqlite3.Row, consumed: set[tuple[str, str, str, str]]
) -> str:
    entity_id = row["id"]
    lines = ["## 4. Everything Else, With Provenance\n"]

    # Include weak (e.g. `mentioned_in`) edges too — §4 is the catch-all for
    # "everything the graph knows about this person", not just strong edges.
    rels_from = _rel_query(conn, from_id=entity_id, include_weak=True)
    rels_to = _rel_query(conn, to_id=entity_id, include_weak=True)
    by_type: dict[str, list[str]] = {}
    for rel in rels_from + rels_to:
        if _rel_key(rel) in consumed:
            continue
        rtype = rel["type"]
        if rel["from_id"] == entity_id:
            peer_id, peer_name, direction = rel["to_id"], rel["to_name"], "→"
        else:
            peer_id, peer_name, direction = rel["from_id"], rel["from_name"], "←"
        uv = _uv_any(rel["review_status"], _status_of(conn, peer_id))
        props = rel["properties"] if isinstance(rel["properties"], dict) else {}
        prop_bits = ", ".join(
            f"{k}: {v}" for k, v in props.items() if v not in (None, "", [], {})
        )
        source = rel.get("source_file") or ""
        detail_bits = [b for b in (prop_bits, f"source: {source}" if source else "") if b]
        detail = f" — {'; '.join(detail_bits)}" if detail_bits else ""
        by_type.setdefault(rtype, []).append(
            f"  - {direction} {peer_name}{uv} `{peer_id}`{detail}"
        )

    if by_type:
        lines.append("**Relations**")
        for rtype in sorted(by_type):
            lines.append(f"**{rtype}**")
            lines.extend(sorted(set(by_type[rtype])))

    props = _row_props(row)
    skip_keys = {"current_company", "current_role", "location"} | _PROP_SKIP
    prop_lines = []
    for key, value in props.items():
        if key in skip_keys:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        prop_lines.append(f"- **{key}**: {value}")
    if prop_lines:
        lines.append("\n**Other Properties**")
        lines.extend(prop_lines)

    if not by_type and not prop_lines:
        lines.append("(no additional relations or properties found)")
    return "\n".join(lines)


def build_person_dossier(
    conn: sqlite3.Connection,
    entity_id: str,
    budget_tokens: int = DEFAULT_DOSSIER_BUDGET_TOKENS,
) -> str:
    """Assemble a person dossier: identity+role, relationship evidence,
    conversation history (newest first), and everything else with
    provenance. Read-time joins only — no new edges are written.
    """
    row = _get_row(conn, entity_id)
    if not row:
        return f"<!-- entity '{entity_id}' not found in index -->\n"
    if row["type"] != "person":
        return (
            f"<!-- entity '{entity_id}' ({row['name']}) is type={row['type']}, not person; "
            "use build_company_dossier or `synapse brief` -->\n"
        )

    # `consumed` tracks the exact relation instances rendered by §2/§3 (by
    # identity, not just by type) so §4 can safely render every other typed
    # relation the graph knows about this person without duplicating what's
    # already shown — see FIX-20 (introduced_by targeting someone other than
    # `me` was previously dropped entirely because §4 excluded by type name).
    consumed: set[tuple[str, str, str, str]] = set()
    section_1 = _p1_identity(conn, row)
    section_2 = _p2_relationship_evidence(conn, entity_id, consumed)
    section_3 = _p3_conversation_history(conn, entity_id, consumed)
    section_4 = _p4_everything_else(conn, row, consumed)

    sections: list[tuple[int, str, str]] = [
        (1, "Identity & Role", section_1),
        (2, "Relationship Evidence", section_2),
        (3, "Conversation History", section_3),
        (4, "Everything Else", section_4),
    ]

    kept, trimmed = _trim_to_budget(sections, budget_tokens)
    parts = [DOSSIER_HEADER]
    for _, _, text in kept:
        parts.append(text)
    if trimmed:
        parts.append(f"<!-- truncated: {', '.join(trimmed)} -->")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def build_dossier(
    conn: sqlite3.Connection,
    entity_id: str,
    budget_tokens: int = DEFAULT_DOSSIER_BUDGET_TOKENS,
) -> str:
    """Dispatch to the company or person dossier assembler by entity type.

    Only `company` and `person` entities have a dossier surface (DIRECTION
    SS5 G2). Any other type returns an informative comment, not an error —
    callers that need to distinguish "wrong type" from "not found" should
    check the entity's type themselves before calling.
    """
    row = _get_row(conn, entity_id)
    if not row:
        return f"<!-- entity '{entity_id}' not found in index -->\n"
    if row["type"] == "company":
        return build_company_dossier(conn, entity_id, budget_tokens=budget_tokens)
    if row["type"] == "person":
        return build_person_dossier(conn, entity_id, budget_tokens=budget_tokens)
    return (
        f"<!-- entity '{entity_id}' ({row['name']}) is type={row['type']}; "
        "the dossier surface supports company and person entities only — "
        "use `synapse brief` for other entity types. -->\n"
    )
