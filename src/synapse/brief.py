"""Deterministic context-pack briefs for consuming models."""

from __future__ import annotations

import json
from typing import Any

from synapse.owner_context import (
    format_knowledge_notice,
    format_profile_entity,
    owner_context_pointer,
)

try:
    import pysqlite3 as sqlite3
except Exception:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

OWNER_BRIEF_HEADER = (
    "<!-- Digital Synapse owner brief — derived from the index; "
    "regenerate with `synapse brief` -->\n"
    "> **Facts marked (unverified) are machine-imported and unreviewed.**\n"
)

ENTITY_BRIEF_HEADER = (
    "<!-- Digital Synapse entity brief — derived from the index; "
    "regenerate with `synapse brief <ref>` -->\n"
)

# Keys to omit from entity brief properties display (internal/noise)
_PROP_SKIP = {"sensitivity", "source_file"}
# Keys that are list-of-strings sources — show only if meaningful
_PROP_SOURCE_KEYS = {"source"}


def _tok(text: str) -> int:
    """Estimate tokens as char_count // 4."""
    return len(text) // 4


def _uv(status: str) -> str:
    return " (unverified)" if status == "proposed" else ""


def _window(props: dict[str, Any]) -> tuple[str, str]:
    start = props.get("started_on") or props.get("since") or ""
    end = props.get("finished_on") or ""
    return str(start), str(end)


def _windows_overlap(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """True if two (start, end) date-ish windows overlap. Missing starts never overlap."""
    a_start, a_end = a
    b_start, b_end = b
    if not a_start or not b_start:
        return False
    a_end_v = a_end or "9999"
    b_end_v = b_end or "9999"
    return a_start <= b_end_v and b_start <= a_end_v


def _get_row(conn: sqlite3.Connection, entity_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()


def _row_fm(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["frontmatter"] or "{}")


def _row_props(row: sqlite3.Row) -> dict[str, Any]:
    fm = _row_fm(row)
    p = fm.get("properties") or {}
    return p if isinstance(p, dict) else {}


def relation_evidence_text(rel: dict[str, Any]) -> str:
    """Compact human evidence carried by a relation, if any."""
    props = rel.get("properties") or {}
    if not isinstance(props, dict):
        return ""
    parts = []
    for key in (
        "role",
        "roles",
        "scope",
        "source_locator",
        "source_family_id",
        "since",
        "date",
        "started_on",
        "finished_on",
        "connected_on",
        "channel",
        "note",
        "summary",
        "context",
    ):
        value = props.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value[:3])
        parts.append(f"{key}: {value}")
    source = str(rel.get("source_file") or "").strip()
    if source:
        parts.append(f"source: {source}")
    return "; ".join(parts)


def _rel_query(
    conn: sqlite3.Connection,
    *,
    from_id: str | None = None,
    to_id: str | None = None,
    rel_type: str | None = None,
    include_weak: bool = False,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if from_id is not None:
        clauses.append("r.from_id = ?")
        params.append(from_id)
    if to_id is not None:
        clauses.append("r.to_id = ?")
        params.append(to_id)
    if rel_type is not None:
        clauses.append("r.type = ?")
        params.append(rel_type)
    if not include_weak:
        clauses.append("r.weak = 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT r.*, ef.name AS from_name, et.name AS to_name "
        "FROM relations r "
        "JOIN entities ef ON ef.id = r.from_id "
        "JOIN entities et ON et.id = r.to_id "
        f"{where} ORDER BY r.created_at DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "from_id": r["from_id"],
            "to_id": r["to_id"],
            "type": r["type"],
            "weak": bool(r["weak"]),
            "properties": json.loads(r["properties"] or "{}"),
            "review_status": r["review_status"],
            "created_at": r["created_at"],
            "from_name": r["from_name"],
            "to_name": r["to_name"],
            "source_file": r["source_file"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Owner brief section builders
# ---------------------------------------------------------------------------

def _s1_identity(conn: sqlite3.Connection, owner: sqlite3.Row) -> str:
    props = _row_props(owner)
    name = owner["name"] or "Me"
    role = props.get("current_role") or props.get("role") or ""
    company = props.get("current_company") or props.get("company") or ""
    location = props.get("location") or ""

    if not company:
        rels = _rel_query(conn, from_id="me", rel_type="works_at")
        if rels:
            company = rels[0]["to_name"]

    lines = ["## 1. Identity\n"]
    id_line = f"**{name}** `me`"
    if role and company:
        id_line += f" — {role} @ {company}"
    elif role:
        id_line += f" — {role}"
    elif company:
        id_line += f" @ {company}"
    lines.append(id_line)
    if location:
        lines.append(f"Location: {location}")

    sources = props.get("source") or []
    if isinstance(sources, list) and sources:
        lines.append(f"Data source(s): {', '.join(str(s) for s in sources)}")
    elif isinstance(sources, str) and sources:
        lines.append(f"Data source(s): {sources}")
    return "\n".join(lines)


def _s2_positions(conn: sqlite3.Connection) -> str:
    rels = _rel_query(conn, from_id="me", rel_type="works_at")
    rels += _rel_query(conn, from_id="me", rel_type="former_employee_of")
    if not rels:
        return ""

    def _sort_key(r: dict[str, Any]) -> str:
        props = r["properties"]
        if isinstance(props, dict):
            date = props.get("started_on") or props.get("since") or r.get("created_at") or ""
        else:
            date = r.get("created_at") or ""
        return str(date)

    rels_sorted = sorted(rels, key=_sort_key, reverse=True)

    lines = ["## 2. Positions\n"]
    for rel in rels_sorted:
        company_name = rel["to_name"]
        company_id = rel["to_id"]
        status_row = conn.execute(
            "SELECT review_status FROM entities WHERE id = ?", (company_id,)
        ).fetchone()
        uv = _uv(status_row["review_status"] if status_row else "proposed")
        props = rel["properties"] if isinstance(rel["properties"], dict) else {}
        positions = props.get("positions") if isinstance(props, dict) else []

        if isinstance(positions, list) and positions:
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                title = pos.get("title") or "Unknown role"
                start = pos.get("started_on") or "?"
                end = pos.get("finished_on") or ("present" if rel["type"] == "works_at" else "?")
                lines.append(f"- **{title}**, {company_name}{uv} `{company_id}` ({start} — {end})")
                details = []
                if pos.get("location"):
                    details.append(f"location: {pos['location']}")
                if isinstance(pos.get("skills"), list) and pos["skills"]:
                    details.append("skills: " + ", ".join(str(s) for s in pos["skills"][:3]))
                if details:
                    lines.append(f"  {'; '.join(details)}")
        else:
            role = props.get("role") or ("present" if rel["type"] == "works_at" else "former")
            start = props.get("started_on") or props.get("since") or "?"
            end = props.get("finished_on") or ("present" if rel["type"] == "works_at" else "?")
            lines.append(f"- **{role}**, {company_name}{uv} `{company_id}` ({start} — {end})")

    return "\n".join(lines)


def _s3_education(conn: sqlite3.Connection) -> str:
    rels = _rel_query(conn, from_id="me", rel_type="attended")
    if not rels:
        return ""

    lines = ["## 3. Education\n"]
    for rel in rels:
        target_name = rel["to_name"]
        target_id = rel["to_id"]
        status_row = conn.execute(
            "SELECT review_status, type FROM entities WHERE id = ?", (target_id,)
        ).fetchone()
        uv = _uv(status_row["review_status"] if status_row else "proposed")
        props = rel["properties"] if isinstance(rel["properties"], dict) else {}
        degree = props.get("degree") or props.get("field_of_study") or ""
        start = props.get("start_date") or props.get("started_on") or ""
        end = props.get("end_date") or props.get("finished_on") or ""
        period = f" ({start} — {end})" if start else ""
        degree_str = f" — {degree}" if degree else ""
        lines.append(f"- {target_name}{uv} `{target_id}`{degree_str}{period}")
    return "\n".join(lines)


def _s4_skills(conn: sqlite3.Connection) -> str:
    rels = _rel_query(conn, from_id="me", rel_type="demonstrates_skill")
    if not rels:
        return ""

    seen: dict[str, str] = {}
    for rel in rels:
        if rel["to_id"] not in seen:
            seen[rel["to_id"]] = rel["to_name"]

    skills_text = ", ".join(seen.values())
    return f"## 4. Skills\n\n{skills_text}"


def _s5_opportunities(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT * FROM entities WHERE type = 'opportunity' ORDER BY updated_at DESC"
    ).fetchall()
    if not rows:
        return ""

    lines = ["## 6. Opportunity Pipeline\n"]
    cap = 20
    shown = 0
    overflow = 0
    for row in rows:
        if shown >= cap:
            overflow += 1
            continue
        props = _row_props(row)
        status = props.get("status") or "unknown"
        role = props.get("role") or row["name"]
        company = props.get("company") or "?"
        uv = _uv(row["review_status"])
        date_str = (props.get("applied_on") or props.get("next_action_on") or "")
        date_part = f" — {date_str}" if date_str else ""
        lines.append(f"- {role} @ {company}{uv} `{row['id']}` [{status}]{date_part}")
        shown += 1
    if overflow:
        lines.append(f"\n_(+ {overflow} more opportunities)_")
    return "\n".join(lines)


def _s5_projects(conn: sqlite3.Connection) -> str:
    rels = _rel_query(conn, from_id="me", rel_type="contributes_to")
    lines = ["## 5. Projects\n"]
    for rel in rels:
        row = _get_row(conn, rel["to_id"])
        if not row or row["type"] != "project":
            continue
        lines.append(f"- {row['name']}{_uv(row['review_status'])} `{row['id']}`")
    return "\n".join(lines) if len(lines) > 1 else ""


def _s6_recruiters(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT * FROM entities WHERE type = 'person'"
    ).fetchall()

    recruiter_rows = []
    for row in rows:
        tags = _row_fm(row).get("tags") or []
        if isinstance(tags, list) and "recruiter" in [str(t).casefold() for t in tags]:
            recruiter_rows.append(row)

    if not recruiter_rows:
        return ""

    lines = ["## 7. Recruiter Contacts\n"]
    by_company: dict[str, list[str]] = {}
    for row in recruiter_rows:
        props = _row_props(row)
        company = props.get("current_company") or props.get("company") or "Unknown"
        uv = _uv(row["review_status"])
        connected = props.get("connected_on") or ""
        connected_str = f" (connected {connected})" if connected else ""

        conv_rels = _rel_query(conn, from_id=row["id"], rel_type="participated_in")
        conv_ptr = ""
        if conv_rels:
            conv_ptr = f" → conversation `{conv_rels[0]['to_id']}`"

        entry = f"{row['name']}{uv} `{row['id']}`{connected_str}{conv_ptr}"
        by_company.setdefault(company, []).append(entry)

    for company in sorted(by_company):
        lines.append(f"**{company}**")
        for entry in by_company[company]:
            lines.append(f"  - {entry}")
    return "\n".join(lines)


def _s7_conversations(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT * FROM entities WHERE type = 'conversation' ORDER BY updated_at DESC LIMIT 15"
    ).fetchall()
    if not rows:
        return ""

    lines = ["## 8. Recent Conversations\n"]
    for row in rows:
        props = _row_props(row)
        date = props.get("date") or (row["created_at"] or "")[:10]
        channel = props.get("channel") or ""
        uv = _uv(row["review_status"])
        channel_str = f" [{channel}]" if channel else ""
        msg_count = props.get("message_count") or ""
        count_str = f", {msg_count} msgs" if msg_count else ""
        lines.append(f"- {row['name']}{uv} `{row['id']}`{channel_str}{count_str} ({date})")
    return "\n".join(lines)


def _s8_goals_insights(conn: sqlite3.Connection) -> str:
    goal_rels = _rel_query(conn, from_id="me", rel_type="has_goal")
    insight_rows = conn.execute(
        "SELECT * FROM entities WHERE type = 'insight' ORDER BY updated_at DESC"
    ).fetchall()

    parts: list[str] = []
    if goal_rels:
        goal_lines = ["### Goals\n"]
        for rel in goal_rels:
            target_id = rel["to_id"]
            status_row = conn.execute(
                "SELECT name, review_status FROM entities WHERE id = ?", (target_id,)
            ).fetchone()
            if status_row:
                uv = _uv(status_row["review_status"])
                goal_lines.append(f"- {status_row['name']}{uv} `{target_id}`")
        parts.append("\n".join(goal_lines))

    if insight_rows:
        ins_lines = ["### Insights\n"]
        for row in insight_rows[:10]:
            uv = _uv(row["review_status"])
            ins_lines.append(f"- {row['name']}{uv} `{row['id']}`")
        parts.append("\n".join(ins_lines))

    if not parts:
        return ""
    return "## 9. Goals & Insights\n\n" + "\n\n".join(parts)


def _s9_footer(conn: sqlite3.Connection) -> str:
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT type, COUNT(*) AS c FROM entities GROUP BY type").fetchall():
        counts[row["type"]] = row["c"]

    extracted: dict[str, str] = {}
    for row in conn.execute("SELECT * FROM entities").fetchall():
        fm = _row_fm(row)
        prov = fm.get("provenance") or {}
        if isinstance(prov, dict):
            src = prov.get("source_file") or ""
            ts = prov.get("extracted_at") or ""
            if src and ts:
                if src not in extracted or ts > extracted[src]:
                    extracted[src] = ts

    lines = ["## 10. Data Footer\n"]
    if extracted:
        lines.append("**Data freshness (latest extracted_at per source):**")
        for src in sorted(extracted):
            lines.append(f"  - `{src}`: {extracted[src]}")
    lines.append("\n**Entity counts:**")
    for etype in sorted(counts):
        lines.append(f"  - {etype}: {counts[etype]}")
    lines.append("\n**Dig deeper:**")
    lines.append("  1. `synapse neighbors <id>` — explore connections from any entity")
    lines.append("  2. `synapse find <text>` — full-text search across the vault")
    lines.append("  3. `synapse brief <ref>` — focused brief on any person, company, or opportunity")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trimming logic
# ---------------------------------------------------------------------------

def _trim_to_budget(
    sections: list[tuple[int, str, str]],  # (priority, name, text)
    budget_tokens: int,
) -> tuple[list[tuple[int, str, str]], list[str]]:
    """Remove lowest-priority sections until under budget. Returns kept + trimmed names."""
    total = sum(_tok(text) for _, _, text in sections)
    if total <= budget_tokens:
        return sorted(sections, key=lambda x: x[0]), []

    # Sort by priority descending (lowest priority first = highest number first)
    ordered = sorted(sections, key=lambda x: x[0], reverse=True)
    trimmed_names: list[str] = []

    for i, (prio, name, text) in enumerate(ordered):
        if prio == 1:
            break  # never trim identity
        total -= _tok(text)
        trimmed_names.append(name)
        ordered[i] = (prio, name, "")
        if total <= budget_tokens:
            break

    kept = [item for item in ordered if item[2]]
    kept_sorted = sorted(kept, key=lambda x: x[0])
    return kept_sorted, trimmed_names


def build_owner_brief(conn: sqlite3.Connection, budget_tokens: int = 8000) -> str:
    """Assemble a token-budgeted owner brief from the index.

    Sections are trimmed lowest-priority-first when over budget.
    Never calls an LLM; deterministic on the same index state.
    """
    owner = _get_row(conn, "me")
    if not owner:
        return "<!-- owner entity 'me' not found in index -->\n"

    raw_sections: list[tuple[int, str, str]] = []

    s1 = _s1_identity(conn, owner)
    context_pointer = owner_context_pointer(conn)
    if context_pointer:
        s1 += "\n\n" + context_pointer
    raw_sections.append((1, "Identity", s1))

    s2 = _s2_positions(conn)
    if s2:
        raw_sections.append((2, "Positions", s2))

    s3 = _s3_education(conn)
    if s3:
        raw_sections.append((3, "Education", s3))

    s4 = _s4_skills(conn)
    if s4:
        raw_sections.append((4, "Skills", s4))

    projects = _s5_projects(conn)
    if projects:
        raw_sections.append((5, "Projects", projects))

    s5 = _s5_opportunities(conn)
    if s5:
        raw_sections.append((6, "Opportunity Pipeline", s5))

    s6 = _s6_recruiters(conn)
    if s6:
        raw_sections.append((7, "Recruiter Contacts", s6))

    s7 = _s7_conversations(conn)
    if s7:
        raw_sections.append((8, "Recent Conversations", s7))

    s8 = _s8_goals_insights(conn)
    if s8:
        raw_sections.append((9, "Goals & Insights", s8))

    s9 = _s9_footer(conn)
    if s9:
        raw_sections.append((10, "Data Footer", s9))

    kept, trimmed = _trim_to_budget(raw_sections, budget_tokens)

    parts = [OWNER_BRIEF_HEADER]
    for _, _, text in kept:
        parts.append(text)

    if trimmed:
        parts.append(f"<!-- truncated: {', '.join(trimmed)} -->")

    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Entity brief
# ---------------------------------------------------------------------------

def build_entity_brief(
    conn: sqlite3.Connection,
    entity_id: str,
    budget_tokens: int = 4000,
) -> str:
    """Assemble a focused brief for a single entity.

    Covers: identity/provenance, properties, relations (grouped), shared context
    with the owner, and body excerpt — all within budget_tokens.
    """
    profile_card = format_profile_entity(conn, entity_id, budget_chars=max(1, budget_tokens * 4))
    if profile_card is not None:
        return profile_card
    row = _get_row(conn, entity_id)
    if not row:
        return f"<!-- entity '{entity_id}' not found in index -->\n"

    fm = _row_fm(row)
    props = fm.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    prov = fm.get("provenance") or {}
    if not isinstance(prov, dict):
        prov = {}

    parts: list[str] = [ENTITY_BRIEF_HEADER]
    notice = format_knowledge_notice(conn, entity_id, budget_chars=max(1, min(1500, budget_tokens * 2)))
    if notice:
        parts.append(notice)

    # --- Identity / provenance line ---
    uv = _uv(row["review_status"])
    prov_src = prov.get("source_file") or ""
    prov_str = f" | source: `{prov_src}`" if prov_src else ""
    parts.append(
        f"**{row['name']}** `{row['id']}`  \n"
        f"Type: {row['type']} | Status: {row['review_status']}{uv}{prov_str}"
    )

    # --- Properties ---
    prop_lines: list[str] = []
    for key, value in props.items():
        if key in _PROP_SKIP:
            continue
        if key in _PROP_SOURCE_KEYS:
            # Only show if non-trivial
            if isinstance(value, list):
                value = [v for v in value if v and str(v).strip()]
                if not value:
                    continue
            elif not value:
                continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        prop_lines.append(f"- **{key}**: {value}")
    if prop_lines:
        parts.append("### Properties\n\n" + "\n".join(prop_lines))

    # --- Relations grouped by type ---
    rels_from = _rel_query(conn, from_id=entity_id)
    rels_to = _rel_query(conn, to_id=entity_id)
    all_rels = rels_from + rels_to
    if all_rels:
        by_type: dict[str, list[str]] = {}
        for rel in all_rels:
            rtype = rel["type"]
            if rel["from_id"] == entity_id:
                peer_id = rel["to_id"]
                peer_name = rel["to_name"]
                direction = "→"
            else:
                peer_id = rel["from_id"]
                peer_name = rel["from_name"]
                direction = "←"

            rel_props = rel["properties"] if isinstance(rel["properties"], dict) else {}
            date = rel_props.get("started_on") or rel_props.get("date") or rel.get("created_at") or ""
            evidence = relation_evidence_text(rel)
            date_str = f" ({date[:10]})" if date and not evidence else ""
            evidence_str = f" — {evidence}" if evidence else ""
            peer_uv = _uv(rel["review_status"])
            entry = f"{direction} {peer_name}{peer_uv} `{peer_id}`{date_str}{evidence_str}"
            by_type.setdefault(rtype, []).append(entry)

        rel_lines = ["### Relations\n"]
        for rtype in sorted(by_type):
            rel_lines.append(f"**{rtype}**")
            for entry in by_type[rtype]:
                rel_lines.append(f"  - {entry}")
        parts.append("\n".join(rel_lines))

    # --- Shared context with owner ---
    owner_works_at = {r["to_id"] for r in _rel_query(conn, from_id="me", rel_type="works_at")}
    owner_attended = {r["to_id"] for r in _rel_query(conn, from_id="me", rel_type="attended")}

    entity_works_at = {r["to_id"] for r in _rel_query(conn, from_id=entity_id, rel_type="works_at")}
    entity_attended = {r["to_id"] for r in _rel_query(conn, from_id=entity_id, rel_type="attended")}

    shared_employer_ids = owner_works_at & entity_works_at
    shared_school_ids = owner_attended & entity_attended

    shared_lines: list[str] = []
    for emp_id in shared_employer_ids:
        emp_name = _get_row(conn, emp_id)
        if emp_name:
            shared_lines.append(f"- Both connected to `{emp_id}` ({emp_name['name']}) via `works_at`")
    for sch_id in shared_school_ids:
        sch_row = _get_row(conn, sch_id)
        if sch_row:
            shared_lines.append(f"- Both connected to `{sch_id}` ({sch_row['name']}) via `attended`")

    if shared_lines:
        parts.append("### Shared Context with Owner\n\n" + "\n".join(shared_lines))

    # --- Body excerpt (to budget) ---
    body = row["body"] or ""
    if body.strip():
        parts.append("### Body Excerpt\n\n" + body.strip())

    # Trim body if over budget
    full_text = "\n\n".join(parts)
    if _tok(full_text) > budget_tokens:
        # Trim body excerpt
        body_budget = budget_tokens - _tok("\n\n".join(parts[:-1]))
        if body_budget > 10:
            max_chars = body_budget * 4
            trimmed_body = body.strip()[:max_chars]
            parts[-1] = "### Body Excerpt\n\n" + trimmed_body + "\n<!-- truncated -->"
        else:
            parts.pop()
            parts.append("<!-- body truncated: over budget -->")

    result = "\n\n".join(parts) + "\n"
    max_chars = max(1, budget_tokens * 4)
    suffix = "\n<!-- truncated: increase brief budget -->"
    if len(result) > max_chars:
        return (result[:max(0, max_chars - len(suffix))] + suffix)[:max_chars]
    return result
