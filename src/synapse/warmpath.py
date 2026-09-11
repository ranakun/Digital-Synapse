"""Deterministic, evidence-ranked connector scoring — "who is my best path
into X, and why."

This is the ONE ranking code path shared by the CLI (`synapse warm-path`),
the MCP tool (`synapse_warm_path`), and G2's dossier warm-entry-points
section. Do not fork or copy-paste this scorer.

Deliberately NOT raw BFS/shortest-path: every graph path runs through the
owner (a hub), so hop-count alone is a useless signal here. Instead this
ranks candidates by additive, hand-commented weights over EXISTING relation
properties only — conversation depth/recency, shared events, introduction
history, shared employers, endorsements, and geography. No ML, no learned
weights, no LLM prose. All weights are monotonic (more/stronger signal never
lowers a score) so same input -> same bytes, always.

No new edges are ever written — every signal is a read-time join over
`relations`/`entities` via `brief.py`'s `_rel_query`/`_get_row` helpers.

HARD INVARIANT: a candidate is only emitted if it has at least one evidence
line. An unexplained score is a defect (see tests/test_warmpath.py).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

try:
    import pysqlite3 as sqlite3
except Exception:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

from synapse.brief import _get_row, _rel_query, _row_props, _window, _windows_overlap
from synapse.util import normalize_name

# ---------------------------------------------------------------------------
# Scoring weights — additive, monotonic, hand-set. Mirrors enrichment.py's
# style: one named constant per signal, one comment each, no magic numbers
# inline. Every constant is positive, so adding/strengthening a signal can
# only raise a candidate's score, never lower it.
# ---------------------------------------------------------------------------

CONVERSATION_BASE_WEIGHT = 15  # any recorded conversation with the owner at all
UNRECORDED_INTERACTION_WEIGHT = 15  # a separate call/meeting without a recorded transcript
CONVERSATION_MESSAGE_UNIT = 5  # +1 score per this many messages exchanged with the owner
CONVERSATION_DEPTH_CAP = 10  # cap on the message-count contribution (mirrors enrichment.py's cap)
RECENCY_WEIGHT_FRESH = 10  # latest conversation with the owner was <=180 days ago
RECENCY_WEIGHT_RECENT = 7  # latest conversation with the owner was <=365 days ago
RECENCY_WEIGHT_STALE = 4  # latest conversation with the owner was <=730 days ago
RECENCY_WEIGHT_OLD = 1  # latest conversation with the owner was >730 days ago (stale beats nothing)
DIRECT_TARGET_CONNECTION_WEIGHT = 25  # candidate is directly `knows`/`introduced_by`/etc. the target itself
CURRENT_AT_TARGET_WEIGHT = 20  # (company target) candidate currently works there — the entry point itself
FORMER_AT_TARGET_WEIGHT = 14  # (company target) candidate formerly worked there — weaker but real
SHARED_EMPLOYER_WITH_TARGET_WEIGHT = 18  # (person target) candidate shares an employer with the target
SHARED_EMPLOYER_OVERLAP_BONUS = 6  # candidate's tenure at that shared employer overlaps the target's
SHARED_VENUE_WITH_TARGET_WEIGHT = 16  # (person target) candidate attended/met_at the same event/school
SHARED_EVENT_WITH_OWNER_WEIGHT = 8  # candidate also met the owner in person at some event/school
INTRODUCTION_BROKER_WEIGHT = 12  # candidate has a proven track record introducing other contacts
INTRODUCED_OWNER_WEIGHT = 10  # candidate personally introduced the owner (on top of broker credit above)
ENDORSEMENT_WEIGHT = 6  # candidate and owner have endorsed each other's skills (a real mutual signal)
GEOGRAPHY_WEIGHT = 3  # deliberately the smallest weight — a bare location match is the weakest evidence
# (see GQ-023: evidence-over-keyword — geography alone must never outrank a real interaction signal)

# Relation types treated as a "direct connection" between two people for the
# direct-target-connection signal (person targets only).
_DIRECT_PERSON_RELATIONS = ("knows", "introduced_by", "collaborates_with", "met_at")


def _parse_date(value: Any) -> str:
    """Best-effort YYYY-MM-DD prefix for date-ish comparison/sorting; '' if absent/unparseable."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return ""
    return text[:10]


def _recency_weight(latest: str, *, today: date) -> int:
    """Tiered recency bonus — same 180/365/730-day boundaries as enrichment.py."""
    if not latest:
        return 0
    try:
        observed = date.fromisoformat(latest[:10])
    except ValueError:
        return 0
    age = (today - observed).days
    if age < 0:
        age = 0
    if age <= 180:
        return RECENCY_WEIGHT_FRESH
    if age <= 365:
        return RECENCY_WEIGHT_RECENT
    if age <= 730:
        return RECENCY_WEIGHT_STALE
    return RECENCY_WEIGHT_OLD


def _venue_map(conn: sqlite3.Connection, entity_id: str) -> dict[str, str]:
    """venue_id -> relation type ('attended' or 'met_at') for entity_id's events/schools."""
    out: dict[str, str] = {}
    for rel in _rel_query(conn, from_id=entity_id, rel_type="attended"):
        out[rel["to_id"]] = "attended"
    for rel in _rel_query(conn, from_id=entity_id, rel_type="met_at"):
        out.setdefault(rel["to_id"], "met_at")
    return out


def _employer_map(conn: sqlite3.Connection, entity_id: str) -> dict[str, dict[str, Any]]:
    """company_id -> {"kind": "current"|"former", "properties": {...}} for entity_id's employers."""
    out: dict[str, dict[str, Any]] = {}
    for rel in _rel_query(conn, from_id=entity_id, rel_type="works_at"):
        props = rel["properties"] if isinstance(rel["properties"], dict) else {}
        out[rel["to_id"]] = {"kind": "current", "properties": props}
    for rel in _rel_query(conn, from_id=entity_id, rel_type="former_employee_of"):
        if rel["to_id"] in out:
            continue  # prefer the 'current' edge if both somehow exist
        props = rel["properties"] if isinstance(rel["properties"], dict) else {}
        out[rel["to_id"]] = {"kind": "former", "properties": props}
    return out


def outreach_exclusion_sources(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Person id -> canonical insight ids that exclude job-search outreach."""
    sources: dict[str, list[str]] = {}
    rows = conn.execute("SELECT * FROM entities WHERE type = 'insight' ORDER BY id").fetchall()
    for row in rows:
        props = _row_props(row)
        person_ids = props.get("excluded_outreach_person_ids") or []
        if not isinstance(person_ids, list):
            continue
        for person_id in person_ids:
            value = str(person_id).strip()
            if value:
                sources.setdefault(value, []).append(row["id"])
    return sources


def rank_connectors(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    limit: int = 10,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Rank candidate connectors for reaching `target_id` (a company or a person).

    Returns a list of {"id", "name", "score", "evidence": [str, ...]} sorted
    by score desc, then normalized name, then id — capped at `limit`. Every
    candidate carries at least one evidence line explaining its score (hard
    invariant; see module docstring). Returns [] if target_id does not exist
    or is not a company/person.

    Deterministic given the same index state and `today`; no randomness, no
    model calls. This is the single ranking implementation shared by the
    `warm-path` CLI command, the `synapse_warm_path` MCP tool, and the
    dossier's "Recommended Warm Entry Points" section.
    """
    current_date = today or datetime.now(UTC).date()
    target_row = _get_row(conn, target_id)
    if not target_row or target_row["type"] not in ("company", "person"):
        return []
    target_type = target_row["type"]

    employer_target = _employer_map(conn, target_id) if target_type == "person" else {}
    employer_target_ids: set[str] = set(employer_target)
    if target_type == "company":
        employer_target_ids.add(target_id)
    venue_target_ids = set(_venue_map(conn, target_id)) if target_type == "person" else set()

    # --- candidate pool: everyone reachable via a signal-bearing relation ---
    candidate_ids: set[str] = set()

    if target_type == "person":
        for rtype in _DIRECT_PERSON_RELATIONS:
            for rel in _rel_query(conn, from_id=target_id, rel_type=rtype):
                candidate_ids.add(rel["to_id"])
            for rel in _rel_query(conn, to_id=target_id, rel_type=rtype):
                candidate_ids.add(rel["from_id"])

    for company_id in employer_target_ids:
        for rel in _rel_query(conn, to_id=company_id, rel_type="works_at"):
            candidate_ids.add(rel["from_id"])
        for rel in _rel_query(conn, to_id=company_id, rel_type="former_employee_of"):
            candidate_ids.add(rel["from_id"])

    for venue_id in venue_target_ids:
        for rel in _rel_query(conn, to_id=venue_id, rel_type="attended"):
            candidate_ids.add(rel["from_id"])
        for rel in _rel_query(conn, to_id=venue_id, rel_type="met_at"):
            candidate_ids.add(rel["from_id"])

    candidate_ids.difference_update(outreach_exclusion_sources(conn))
    candidate_ids.discard("me")
    candidate_ids.discard(target_id)

    target_props = _row_props(target_row)
    target_location = str(target_props.get("location") or "").strip().casefold()

    owner_conv_ids = {r["to_id"] for r in _rel_query(conn, from_id="me", rel_type="participated_in")}
    owner_venue_ids = set(_venue_map(conn, "me"))

    scored: list[dict[str, Any]] = []
    for candidate_id in sorted(candidate_ids):
        row = _get_row(conn, candidate_id)
        if not row or row["type"] != "person":
            continue

        score = 0
        evidence: list[str] = []

        # -- signals: recorded conversations and separate unrecorded interactions --
        candidate_conv_ids = {r["to_id"] for r in _rel_query(conn, from_id=candidate_id, rel_type="participated_in")}
        shared_conv_ids = candidate_conv_ids & owner_conv_ids
        has_int = bool(_rel_query(conn, from_id="me", to_id=candidate_id, rel_type="has_interaction"))
        if shared_conv_ids:
            message_count = 0
            latest = ""
            for conv_id in shared_conv_ids:
                conv_row = _get_row(conn, conv_id)
                if not conv_row:
                    continue
                cprops = _row_props(conv_row)
                message_count += int(cprops.get("message_count") or 0)
                conv_date = _parse_date(
                    cprops.get("last_message_at") or cprops.get("date") or conv_row["created_at"]
                )
                if conv_date > latest:
                    latest = conv_date
            conv_score = CONVERSATION_BASE_WEIGHT
            if message_count:
                conv_score += min(CONVERSATION_DEPTH_CAP, message_count // CONVERSATION_MESSAGE_UNIT)
            if latest:
                conv_score += _recency_weight(latest, today=current_date)
            score += conv_score
            detail = f"{message_count} message(s) across {len(shared_conv_ids)} recorded conversation(s) with the owner"
            if latest:
                detail += f", last {latest}"
            evidence.append(f"{detail} (+{conv_score})")

        if has_int:
            score += UNRECORDED_INTERACTION_WEIGHT
            evidence.append(
                "separate call/meeting without a recorded transcript on record with the owner "
                f"(+{UNRECORDED_INTERACTION_WEIGHT})"
            )

        # -- signal: directly connected to the target (person targets only) --
        if target_type == "person":
            direct_types = [
                rtype
                for rtype in _DIRECT_PERSON_RELATIONS
                if _rel_query(conn, from_id=candidate_id, to_id=target_id, rel_type=rtype)
                or _rel_query(conn, from_id=target_id, to_id=candidate_id, rel_type=rtype)
            ]
            if direct_types:
                add = DIRECT_TARGET_CONNECTION_WEIGHT
                score += add
                rels_str = ", ".join(f"`{t}`" for t in direct_types)
                evidence.append(f"directly connected to the target via {rels_str} (+{add})")

        # -- signal: shared employer with the target --
        candidate_employers = _employer_map(conn, candidate_id)
        shared_company_ids = set(candidate_employers) & employer_target_ids
        for company_id in sorted(shared_company_ids):
            crow = _get_row(conn, company_id)
            company_name = crow["name"] if crow else company_id
            kind = candidate_employers[company_id]["kind"]
            cprops = candidate_employers[company_id]["properties"]
            start, end = _window(cprops)
            end_label = (end or "present") if kind == "current" else (end or "?")
            window_str = f" ({start} — {end_label})" if start else ""
            if target_type == "company":
                weight = CURRENT_AT_TARGET_WEIGHT if kind == "current" else FORMER_AT_TARGET_WEIGHT
                label = "currently works at the target" if kind == "current" else "formerly worked at the target"
                score += weight
                evidence.append(f"{label}{window_str} (+{weight})")
            else:
                score += SHARED_EMPLOYER_WITH_TARGET_WEIGHT
                evidence.append(
                    f"shared employer {company_name}{window_str} — shared employment context with the target "
                    f"(+{SHARED_EMPLOYER_WITH_TARGET_WEIGHT})"
                )
                target_window = _window(employer_target.get(company_id, {}).get("properties", {}))
                if _windows_overlap(_window(cprops), target_window):
                    score += SHARED_EMPLOYER_OVERLAP_BONUS
                    evidence.append(
                        f"tenure at {company_name} overlaps the target's own tenure there "
                        f"(+{SHARED_EMPLOYER_OVERLAP_BONUS})"
                    )

        # -- signal: shared event/school with the target (person targets only) --
        candidate_venues = _venue_map(conn, candidate_id)
        if target_type == "person":
            shared_venue_ids = set(candidate_venues) & venue_target_ids
            for venue_id in sorted(shared_venue_ids):
                vrow = _get_row(conn, venue_id)
                vname = vrow["name"] if vrow else venue_id
                label = "event" if vrow and vrow["type"] == "event" else "school"
                score += SHARED_VENUE_WITH_TARGET_WEIGHT
                evidence.append(
                    f"attended {vname} ({label}) together with the target (+{SHARED_VENUE_WITH_TARGET_WEIGHT})"
                )

        # -- signal: shared event/school with the OWNER (a real in-person tie) --
        shared_owner_venue_ids = set(candidate_venues) & owner_venue_ids
        for venue_id in sorted(shared_owner_venue_ids):
            vrow = _get_row(conn, venue_id)
            vname = vrow["name"] if vrow else venue_id
            score += SHARED_EVENT_WITH_OWNER_WEIGHT
            evidence.append(f"also connected to the owner via {vname} (+{SHARED_EVENT_WITH_OWNER_WEIGHT})")

        # -- signal: candidate is a proven introduction broker --
        broker_rels = _rel_query(conn, to_id=candidate_id, rel_type="introduced_by")
        if broker_rels:
            add = INTRODUCTION_BROKER_WEIGHT
            score += add
            n = len(broker_rels)
            plural = "s" if n != 1 else ""
            evidence.append(f"has introduced {n} contact{plural} before (proven connector) (+{add})")
        if _rel_query(conn, from_id="me", to_id=candidate_id, rel_type="introduced_by"):
            score += INTRODUCED_OWNER_WEIGHT
            evidence.append(f"personally introduced the owner (+{INTRODUCED_OWNER_WEIGHT})")

        # -- signal: endorsement / mutual vouching with the owner --
        endorsements = _rel_query(conn, from_id=candidate_id, to_id="me", rel_type="endorsed_skill")
        endorsements += _rel_query(conn, from_id="me", to_id=candidate_id, rel_type="endorsed_skill")
        if endorsements:
            add = ENDORSEMENT_WEIGHT
            score += add
            skills = sorted({str((e["properties"] or {}).get("skill_name") or e["to_name"]) for e in endorsements})
            evidence.append(f"endorsement on record ({', '.join(skills[:3])}) (+{add})")

        # -- signal: geography (deliberately the smallest weight; see GQ-023) --
        candidate_location = str(_row_props(row).get("location") or "").strip().casefold()
        if target_location and candidate_location and (
            target_location in candidate_location or candidate_location in target_location
        ):
            score += GEOGRAPHY_WEIGHT
            raw_location = _row_props(row).get("location")
            evidence.append(f"shares a location with the target ({raw_location}) (+{GEOGRAPHY_WEIGHT})")

        if not evidence:
            continue  # HARD INVARIANT: never emit a candidate without evidence

        scored.append({
            "id": candidate_id,
            "name": row["name"],
            "review_status": row["review_status"],
            "score": score,
            "evidence": evidence,
        })

    scored.sort(key=lambda c: (-c["score"], normalize_name(c["name"]), c["id"]))
    return scored[:limit]
