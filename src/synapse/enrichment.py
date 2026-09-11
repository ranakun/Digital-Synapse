"""Deterministic career-data enrichment queue and LinkedIn profile importer."""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from synapse.config import load_config, resolve_vault
from synapse.extractors import extract_pdf
from synapse.importers import (
    _company_body,
    _company_key,
    _entity_index,
    _managed_section,
    _merge_aliases,
    _merge_properties,
    _merge_tags,
    _read_or_create_entity,
    _resolve_company_display,
)
from synapse.index import connect, reindex
from synapse.parser import entity_files
from synapse.util import normalize_name, read_frontmatter, utc_now, write_frontmatter

PROFILE_IMPORTER = "deterministic:linkedin-profile-pdf"
QUEUE_VERSION = 1
QUEUE_STATES = {"pending", "captured", "imported", "skipped", "needs-review"}

DEFAULT_KEYWORDS = (
    "custody",
    "mpc",
    "wallet",
    "cryptography",
    "crypto",
    "security",
    "blockchain",
    "web3",
    "artificial intelligence",
    "machine learning",
    " ai ",
)
DEFAULT_GEOGRAPHIES = (
    "bengaluru",
    "bangalore",
    "india",
    "singapore",
    "dubai",
    "united arab emirates",
    "remote",
)
RECRUITER_TERMS = ("recruit", "talent", "sourcing", "staffing", "headhunt", "hiring")
RELEVANT_RELATIONS = {
    "participated_in",
    "has_interaction",
    "interviewed_for",
    "referred_for",
    "recruits_for",
    "works_at",
    "former_employee_of",
    "attended",
}


@dataclass(frozen=True, slots=True)
class ProfilePosition:
    company: str
    title: str
    started_on: str
    finished_on: str = ""
    location: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class ProfileEducation:
    school: str
    degree: str = ""
    started_on: str = ""
    finished_on: str = ""
    details: str = ""


@dataclass(frozen=True, slots=True)
class LinkedInProfileSnapshot:
    headline: str = ""
    location: str = ""
    positions: tuple[ProfilePosition, ...] = ()
    education: tuple[ProfileEducation, ...] = ()
    certifications: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    raw_text: str = ""
    warnings: tuple[str, ...] = ()


def _queue_path(root: Path, output: str | Path | None = None) -> Path:
    if output is not None:
        path = Path(output)
        return path if path.is_absolute() else root / path
    return root / "inbox" / "enrichment" / "linkedin-queue.yaml"


def _load_yaml_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _write_yaml_if_changed(path: Path, data: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _frontmatter(row: Any, key: str = "frontmatter") -> dict[str, Any]:
    value = yaml.safe_load(row[key] or "{}") or {}
    return value if isinstance(value, dict) else {}


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _interaction_recency_score(value: Any, *, today: date) -> int:
    observed = _parse_iso_date(value)
    if observed is None:
        return 0
    age = (today - observed).days
    if age <= 180:
        return 10
    if age <= 365:
        return 7
    if age <= 730:
        return 4
    return 1


def generate_enrichment_queue(
    vault: str | Path | None = None,
    *,
    limit: int = 50,
    keywords: list[str] | None = None,
    geographies: list[str] | None = None,
    output: str | Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Rank LinkedIn contacts deterministically and write vault-local queue state."""

    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    root = resolve_vault(vault)
    cfg = load_config(root)
    enrichment_cfg = cfg.get("career_enrichment") or {}
    active_keywords = tuple(
        str(item).casefold()
        for item in (keywords or enrichment_cfg.get("keywords") or DEFAULT_KEYWORDS)
        if str(item).strip()
    )
    active_geographies = tuple(
        str(item).casefold()
        for item in (geographies or enrichment_cfg.get("preferred_geographies") or DEFAULT_GEOGRAPHIES)
        if str(item).strip()
    )
    current_date = today or datetime.now(UTC).date()

    reindex(root)
    conn = connect(root)
    try:
        people_rows = conn.execute(
            "SELECT id, name, frontmatter FROM entities WHERE type = 'person' ORDER BY id"
        ).fetchall()
        relation_rows = conn.execute(
            "SELECT r.from_id, r.to_id, r.type, r.properties, "
            "e.type AS target_type, e.name AS target_name, e.frontmatter AS target_frontmatter "
            "FROM relations r JOIN entities e ON e.id = r.to_id "
            "WHERE r.type IN ({}) ORDER BY r.from_id, r.type, r.to_id".format(
                ",".join("?" for _ in sorted(RELEVANT_RELATIONS))
            ),
            tuple(sorted(RELEVANT_RELATIONS)),
        ).fetchall()
    finally:
        conn.close()

    relation_facts: dict[str, list[dict[str, Any]]] = {}
    for row in relation_rows:
        target_fm = _frontmatter(row, "target_frontmatter")
        target_props = target_fm.get("properties") or {}
        if not isinstance(target_props, dict):
            target_props = {}
        relation_facts.setdefault(str(row["from_id"]), []).append(
            {
                "relation": str(row["type"]),
                "target_id": str(row["to_id"]),
                "target_type": str(row["target_type"]),
                "target_name": str(row["target_name"]),
                "properties": target_props,
            }
        )

    candidates: list[dict[str, Any]] = []
    for row in people_rows:
        person_id = str(row["id"])
        if person_id == "me":
            continue
        fm = _frontmatter(row)
        props = fm.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        linkedin_url = str(props.get("linkedin_url") or "").strip()
        if not linkedin_url:
            continue

        name = str(row["name"])
        role = str(props.get("current_role") or props.get("role") or "")
        company = str(props.get("current_company") or props.get("company") or "")
        location = str(props.get("location") or props.get("current_location") or "")
        tags = [str(tag) for tag in fm.get("tags") or []]
        facts = relation_facts.get(person_id, [])
        score = 0
        reasons: list[str] = []
        interaction_summary: list[dict[str, Any]] = []

        role_haystack = f" {role} {' '.join(tags)} ".casefold()
        if "recruiter" in {tag.casefold() for tag in tags} or any(
            term in role_haystack for term in RECRUITER_TERMS
        ):
            score += 30
            reasons.append("Recruiter or talent role (+30)")

        career_haystack = f" {role} {company} {' '.join(tags)} ".casefold()
        keyword_hits = sorted(
            {
                keyword.strip()
                for keyword in active_keywords
                if keyword and keyword in career_haystack
            }
        )
        if keyword_hits:
            keyword_score = min(24, 4 * len(keyword_hits))
            score += keyword_score
            reasons.append(
                f"Relevant role/company keywords: {', '.join(keyword_hits)} (+{keyword_score})"
            )

        conversations = [
            fact
            for fact in facts
            if fact["relation"] == "participated_in" and fact["target_type"] == "conversation"
        ]
        if conversations:
            message_count = sum(int(fact["properties"].get("message_count") or 0) for fact in conversations)
            latest = max(
                (
                    str(
                        fact["properties"].get("last_message_at")
                        or fact["properties"].get("date")
                        or ""
                    )
                    for fact in conversations
                ),
                default="",
            )
            depth_score = min(10, message_count // 5)
            recency_score = _interaction_recency_score(latest, today=current_date)
            score += 15 + depth_score + recency_score
            reasons.append(
                f"{len(conversations)} prior conversation(s), {message_count} messages "
                f"(+{15 + depth_score + recency_score})"
            )
            interaction_summary.append(
                {
                    "kind": "conversation",
                    "count": len(conversations),
                    "message_count": message_count,
                    "last_interaction_at": latest or None,
                }
            )

        opportunity_facts = [
            fact
            for fact in facts
            if fact["relation"] in {"has_interaction", "interviewed_for", "referred_for"}
            and fact["target_type"] == "opportunity"
        ]
        if opportunity_facts:
            opportunity_score = min(24, 12 + 4 * len(opportunity_facts))
            score += opportunity_score
            reasons.append(
                f"Prior opportunity/referral evidence: {len(opportunity_facts)} (+{opportunity_score})"
            )
            interaction_summary.extend(
                {
                    "kind": fact["relation"],
                    "entity_id": fact["target_id"],
                    "name": fact["target_name"],
                }
                for fact in opportunity_facts[:5]
            )

        if any(fact["relation"] == "attended" for fact in facts) or "alumni" in {
            tag.casefold() for tag in tags
        }:
            score += 8
            reasons.append("Alumni evidence (+8)")

        geography_hits = sorted(
            geography for geography in active_geographies if geography in location.casefold()
        )
        if geography_hits:
            score += 8
            reasons.append(f"Preferred geography: {', '.join(geography_hits)} (+8)")

        has_history = any(fact["relation"] == "former_employee_of" for fact in facts)
        if not has_history and not props.get("linkedin_profile_snapshot_at"):
            score += 10
            reasons.append("Missing employment history (+10)")

        connected_score = _interaction_recency_score(props.get("connected_on"), today=current_date)
        if connected_score:
            connection_points = min(6, connected_score)
            score += connection_points
            reasons.append(f"Recent LinkedIn connection (+{connection_points})")

        candidates.append(
            {
                "person_id": person_id,
                "name": name,
                "linkedin_url": linkedin_url,
                "score": score,
                "ranking_reasons": reasons,
                "current_company": company or None,
                "current_role": role or None,
                "interaction_facts": interaction_summary,
            }
        )

    candidates.sort(key=lambda item: (-int(item["score"]), normalize_name(item["name"]), item["person_id"]))
    selected = candidates[:limit]
    path = _queue_path(root, output)
    existing = _load_yaml_map(path)
    existing_items = {
        str(item.get("person_id")): item
        for item in existing.get("items") or []
        if isinstance(item, dict) and item.get("person_id")
    }
    operational_keys = (
        "state",
        "local_source_filename",
        "captured_at",
        "imported_at",
        "owner_note",
        "parse_warnings",
    )
    for item in selected:
        old = existing_items.get(item["person_id"], {})
        for key in operational_keys:
            if key in old:
                item[key] = old[key]
        item.setdefault("state", "pending")
        item.setdefault("local_source_filename", None)
        item.setdefault("captured_at", None)
        item.setdefault("owner_note", None)

    queue = {
        "version": QUEUE_VERSION,
        "options": {
            "limit": limit,
            "keywords": list(active_keywords),
            "preferred_geographies": list(active_geographies),
        },
        "items": selected,
    }
    _write_yaml_if_changed(path, queue)
    return {"queue": str(path), "items": len(selected), "ranked": selected}


def attach_linkedin_profile(
    source: str | Path,
    *,
    person_id: str,
    captured_at: str,
    vault: str | Path | None = None,
    queue: str | Path | None = None,
) -> dict[str, Any]:
    """Attach a PDF to one explicit queue item without guessing identity."""
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "attach_linkedin_profile")

    root = resolve_vault(vault)
    source_path = Path(source).resolve()
    if source_path.suffix.casefold() != ".pdf":
        raise ValueError("LinkedIn profile attachment must be a PDF")
    _require_capture_date(captured_at)
    queue_path = _queue_path(root, queue)
    data = _load_yaml_map(queue_path)
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    item = next((candidate for candidate in items if str(candidate.get("person_id")) == person_id), None)
    if item is None:
        raise ValueError(f"Person {person_id!r} is not present in {queue_path}")

    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    destination = root / "inbox" / "linkedin-profiles" / f"{person_id}-{digest[:12]}.pdf"
    _copy_if_needed(source_path, destination)
    item["state"] = "captured"
    item["local_source_filename"] = destination.relative_to(root).as_posix()
    item["captured_at"] = captured_at
    item.pop("parse_warnings", None)
    _write_yaml_if_changed(queue_path, data)
    return {
        "person_id": person_id,
        "queue": str(queue_path),
        "source": str(destination),
        "captured_at": captured_at,
    }


def _require_capture_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("captured_at must use YYYY-MM-DD") from exc


def _copy_if_needed(source: Path, destination: Path) -> None:
    if destination.exists():
        if destination.read_bytes() != source.read_bytes():
            raise ValueError(f"Refusing to overwrite different source: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


_SECTION_HEADINGS = {
    "contact",
    "top skills",
    "skills",
    "languages",
    "certifications",
    "honors-awards",
    "honors & awards",
    "summary",
    "experience",
    "education",
}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_DATE_TOKEN = r"(?:[A-Za-z]{3,9}\s+\d{4}|\d{4})"
_DATE_RANGE_RE = re.compile(
    rf"^(?P<start>{_DATE_TOKEN})\s*[-\u2013]\s*"
    rf"(?P<end>Present|Current|{_DATE_TOKEN})(?:\s*\([^)]*\))?$",
    flags=re.IGNORECASE,
)


def parse_linkedin_profile_text(
    text: str,
    *,
    expected_name: str = "",
) -> LinkedInProfileSnapshot:
    """Conservatively parse the stable sections of LinkedIn's profile PDF text."""

    raw_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    warnings: list[str] = []

    heading_indexes = {
        line.casefold(): index for index, line in enumerate(lines) if line.casefold() in _SECTION_HEADINGS
    }
    headline = ""
    location = ""
    if expected_name:
        name_index = next(
            (
                index
                for index, line in enumerate(lines)
                if normalize_name(line) == normalize_name(expected_name)
            ),
            None,
        )
        if name_index is not None:
            identity_lines = [
                line
                for line in lines[name_index + 1 : name_index + 4]
                if line.casefold() not in _SECTION_HEADINGS
            ]
            if identity_lines:
                headline = identity_lines[0]
            if len(identity_lines) > 1 and _looks_like_location(identity_lines[1]):
                location = identity_lines[1]
        else:
            warnings.append("Canonical person name was not found in extracted PDF text.")

    experience_lines = _section_lines(lines, heading_indexes, "experience")
    positions, position_warnings = _parse_positions(experience_lines)
    warnings.extend(position_warnings)

    education_lines = _section_lines(lines, heading_indexes, "education")
    education, education_warnings = _parse_education(education_lines)
    warnings.extend(education_warnings)

    certifications = tuple(_simple_section_values(lines, heading_indexes, "certifications"))
    skills_heading = "top skills" if "top skills" in heading_indexes else "skills"
    skills = tuple(_simple_section_values(lines, heading_indexes, skills_heading))
    if not positions:
        warnings.append("No confidently parsed employment positions were found.")

    return LinkedInProfileSnapshot(
        headline=headline,
        location=location,
        positions=tuple(positions),
        education=tuple(education),
        certifications=certifications,
        skills=skills,
        raw_text=raw_text,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _section_lines(
    lines: list[str],
    indexes: dict[str, int],
    heading: str,
) -> list[str]:
    start = indexes.get(heading)
    if start is None:
        return []
    later = sorted(index for index in indexes.values() if index > start)
    end = later[0] if later else len(lines)
    return lines[start + 1 : end]


def _simple_section_values(
    lines: list[str],
    indexes: dict[str, int],
    heading: str,
) -> list[str]:
    return list(dict.fromkeys(_section_lines(lines, indexes, heading)))[:50]


def _parse_date_token(value: str) -> str:
    text = value.strip()
    if re.fullmatch(r"\d{4}", text):
        return text
    match = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{4})", text)
    if not match:
        return ""
    month = _MONTHS.get(match.group(1).casefold())
    return f"{match.group(2)}-{month:02d}" if month else ""


def _date_ranges(lines: list[str]) -> list[tuple[int, re.Match[str]]]:
    return [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _DATE_RANGE_RE.match(line))
    ]


def _parse_positions(lines: list[str]) -> tuple[list[ProfilePosition], list[str]]:
    ranges = _date_ranges(lines)
    positions: list[ProfilePosition] = []
    warnings: list[str] = []
    for range_index, (line_index, match) in enumerate(ranges):
        previous_date = ranges[range_index - 1][0] if range_index else -1
        candidates = lines[max(previous_date + 1, line_index - 3) : line_index]
        # Drop duration / bare-location / description-tail noise so the last two
        # remaining lines are the real (company, title) header, not a stray line.
        header_candidates = [line for line in candidates if not _looks_like_noncompany(line)]
        if len(header_candidates) < 2:
            warnings.append(
                f"Ambiguous experience block near '{lines[line_index]}' "
                "(no clear company/title header)."
            )
            continue
        company, title = header_candidates[-2], header_candidates[-1]
        next_date = ranges[range_index + 1][0] if range_index + 1 < len(ranges) else len(lines)
        post_end = max(line_index + 1, next_date - 2)
        details = lines[line_index + 1 : post_end]
        location = details[0] if details and _looks_like_location(details[0]) else ""
        if location:
            details = details[1:]
        end_text = match.group("end")
        positions.append(
            ProfilePosition(
                company=company,
                title=title,
                started_on=_parse_date_token(match.group("start")),
                finished_on="" if end_text.casefold() in {"present", "current"} else _parse_date_token(end_text),
                location=location,
                description="\n".join(details),
            )
        )
    return positions, warnings


def _parse_education(lines: list[str]) -> tuple[list[ProfileEducation], list[str]]:
    ranges = _date_ranges(lines)
    records: list[ProfileEducation] = []
    warnings: list[str] = []
    for range_index, (line_index, match) in enumerate(ranges):
        previous_date = ranges[range_index - 1][0] if range_index else -1
        candidates = lines[max(previous_date + 1, line_index - 3) : line_index]
        if not candidates:
            warnings.append(f"Ambiguous education block near '{lines[line_index]}'.")
            continue
        school = candidates[-2] if len(candidates) >= 2 else candidates[-1]
        degree = candidates[-1] if len(candidates) >= 2 else ""
        next_date = ranges[range_index + 1][0] if range_index + 1 < len(ranges) else len(lines)
        details = lines[line_index + 1 : max(line_index + 1, next_date - 2)]
        end_text = match.group("end")
        records.append(
            ProfileEducation(
                school=school,
                degree=degree,
                started_on=_parse_date_token(match.group("start")),
                finished_on="" if end_text.casefold() in {"present", "current"} else _parse_date_token(end_text),
                details="\n".join(details),
            )
        )
    return records, warnings


def _looks_like_location(value: str) -> bool:
    text = value.casefold()
    return (
        len(value) <= 100
        and (
            "," in value
            or " area" in text
            or text.endswith((" india", " singapore", " remote"))
            or any(term in text for term in ("united states", "united kingdom", "uae", "dubai"))
        )
    )


# Country / major-region tokens that end a "City, Region, Country" location line.
# Kept deliberately small: a location is only recognised in the header window
# when the FINAL comma-separated segment is one of these, so real companies that
# merely contain a comma ("Cornami, Inc.") or end in a country word ("Samsung
# India") are never mistaken for a place.
_PLACE_TAILS = frozenset(
    {
        "united states", "usa", "us", "canada", "india", "united kingdom",
        "uk", "singapore", "australia", "germany", "france", "israel",
        "china", "hong kong", "netherlands", "switzerland", "japan",
        "california", "new york", "texas", "british columbia",
    }
)


def _header_looks_like_location(value: str) -> bool:
    """Location test tuned for the header window (stricter than _looks_like_location).

    In the header window a bare comma is NOT enough to call a line a location —
    "Cornami, Inc." and "Gavi, the Vaccine Alliance" are real employers. A line
    is a location only when it is an exact bare geography, an airport, an " area"
    string, or a comma-separated place whose FINAL segment is a known
    country/region and which is not a legal-entity name.
    """

    low = value.strip().casefold()
    if low in _BARE_GEOGRAPHIES or low.endswith(" airport"):
        return True
    if low.rstrip(",").endswith(_ORG_SUFFIXES):
        return False
    if "," in value:
        parts = [part.strip().casefold() for part in value.split(",") if part.strip()]
        if len(parts) >= 2 and parts[-1] in _PLACE_TAILS:
            return True
    return " area" in low or "metropolitan" in low


# Bare place names that LinkedIn's Experience section lists on their own line
# (location without a comma), which the header heuristic would otherwise mint as
# an employer. Kept small and exact-match only so real single-word companies are
# never dropped.
_BARE_GEOGRAPHIES = {
    "india",
    "singapore",
    "bengaluru",
    "bangalore",
    "dubai",
    "remote",
    "netherlands",
    "amsterdam",
    "london",
    "chennai",
    "dehradun",
    "united states",
    "united kingdom",
    "uae",
    "israel",
    "hong kong",
    "moscow",
    "tel aviv",
    "los angeles",
    "new york",
    "vancouver",
}
_DURATION_RE = re.compile(
    r"^\d+\s+(?:years?|months?|yrs?|mos?)(?:\s+\d+\s+(?:years?|months?|yrs?|mos?))?$",
    flags=re.IGNORECASE,
)
# PDF pagination footer that leaks into the text stream ("Page 2 of 3").
_PAGINATION_RE = re.compile(r"^page\s+\d+\s+of\s+\d+$", flags=re.IGNORECASE)
# Legal-entity abbreviations that legitimately end a company name with a period.
_ORG_SUFFIXES = (
    "ltd.", "inc.", "corp.", "co.", "llc.", "plc.", "pvt.", "gmbh.",
    "s.a.", "l.p.", "llp.", "pte.",
)
# First word of a wrapped achievement/description line that got pulled into the
# header window. A real employer never opens with a narrative past-tense verb.
# Combined with a length guard below so short real names ("Managed Services")
# are never dropped.
_NARRATIVE_STARTERS = frozenset(
    {
        "left", "improved", "laid", "managed", "led", "built", "developed",
        "designed", "created", "achieved", "delivered", "drove", "established",
        "spearheaded", "oversaw", "coordinated", "responsible", "worked",
        "handled", "supported", "provided", "ensured", "implemented", "reduced",
        "increased", "collaborated", "main", "focused", "focus", "helped",
        "grew", "owned", "maintained", "analyzed", "conducted", "identified",
    }
)


def _looks_like_duration(value: str) -> bool:
    return bool(_DURATION_RE.match(value.strip()))


def _looks_like_noncompany(value: str) -> bool:
    """True when a line cannot be a real employer/title header.

    LinkedIn's PDF Experience section interleaves duration lines ("2 years
    3 months"), bare locations ("Singapore"), and wrapped description tails
    ("...and product development.") with the company/title headers. The old
    heuristic — take the two lines immediately before a date range as
    (company, title) — captured those noise lines as employers and minted junk
    company entities. This filter drops them from the header window.
    """

    text = value.strip()
    if not text:
        return True
    low = text.casefold()
    if _looks_like_duration(text):
        return True
    if _PAGINATION_RE.match(text):
        return True
    # A recognised legal-entity suffix ("Inc.", "Ltd.", "LLC") is a strong signal
    # of a real employer; never drop it from the header window over a comma or a
    # trailing country word.
    if low.rstrip(",").endswith(_ORG_SUFFIXES):
        return False
    if _header_looks_like_location(text):
        return True
    first = text[0]
    # Leading punctuation ("- Drove ...", "• ...") is a wrapped bullet line.
    if not first.isalnum() and (" " in text or text.endswith(".")):
        return True
    # A lowercase-first line is a wrapped sentence continuation only when it
    # reads like prose (long, or period-terminated). Short lowercase-first names
    # are legitimate modern brands ("fija Finance", "myHQ by ANAROCK", "xNerds
    # Solutions") and must be kept.
    if first.islower() and (text.endswith(".") or len(text.split()) >= 5):
        return True
    # Uppercase-start prose that wrapped into the header window: a real company
    # or title never ends in a period while also reading like a sentence. Legal
    # abbreviations ("Pvt. Ltd.", "Inc.", "Corp.") legitimately end in a period,
    # so exempt them or genuine (often Indian) employers get dropped.
    if (
        text.endswith(".")
        and not low.endswith(_ORG_SUFFIXES)
        and ("," in text or " and " in low or len(text.split()) >= 5)
    ):
        return True
    # Uppercase-start narrative *without* a trailing period (LinkedIn truncates
    # wrapped achievement lines mid-sentence). A real employer never opens with a
    # past-tense verb; the length guard keeps short real names ("Managed
    # Services", "Left Bank Records") safe.
    words = text.split()
    if len(words) >= 5 and words[0].casefold() in _NARRATIVE_STARTERS:
        return True
    return False


def _entity_path_by_id(root: Path, entity_id: str) -> Path | None:
    for path in entity_files(root):
        metadata, _ = read_frontmatter(path)
        if str(metadata.get("id") or "") == entity_id:
            return path
    return None


def _snapshot_markdown(
    snapshot: LinkedInProfileSnapshot,
    *,
    captured_at: str,
    source_file: str,
) -> str:
    lines = [
        "## LinkedIn Profile Snapshot",
        "",
        f"- **Captured:** {captured_at}",
        f"- **Source:** `{source_file}`",
    ]
    if snapshot.headline:
        lines.append(f"- **Headline:** {snapshot.headline}")
    if snapshot.location:
        lines.append(f"- **Location:** {snapshot.location}")
    lines.extend(["", "### Employment History", ""])
    if snapshot.positions:
        for position in snapshot.positions:
            end = position.finished_on or "present"
            lines.append(
                f"- **{position.title}**, {position.company} "
                f"({position.started_on or 'unknown'} to {end})"
            )
            if position.location:
                lines.append(f"  - Location: {position.location}")
            for detail in position.description.splitlines():
                if detail.strip():
                    lines.append(f"  - {detail.strip()}")
    else:
        lines.append("_No employment blocks were parsed confidently._")

    lines.extend(["", "### Education", ""])
    if snapshot.education:
        for record in snapshot.education:
            date_text = ""
            if record.started_on or record.finished_on:
                date_text = f" ({record.started_on or 'unknown'} to {record.finished_on or 'present'})"
            degree = f" - {record.degree}" if record.degree else ""
            lines.append(f"- **{record.school}**{degree}{date_text}")
    else:
        lines.append("_No education blocks were parsed confidently._")

    if snapshot.certifications:
        lines.extend(["", "### Certifications", "", *[f"- {item}" for item in snapshot.certifications]])
    if snapshot.skills:
        lines.extend(["", "### Skills", "", *[f"- {item}" for item in snapshot.skills]])
    if snapshot.warnings:
        lines.extend(["", "### Parse Review", "", *[f"- {warning}" for warning in snapshot.warnings]])
    lines.extend(["", "### Extracted Text", ""])
    lines.extend(f"    {line}" if line else "" for line in snapshot.raw_text.splitlines())
    return "\n".join(lines)


def _position_relation_properties(
    positions: list[ProfilePosition],
    *,
    captured_at: str,
) -> dict[str, Any]:
    sorted_positions = sorted(
        positions,
        key=lambda item: (item.started_on, item.finished_on or "9999-99", item.title),
        reverse=True,
    )
    latest = sorted_positions[0]
    result: dict[str, Any] = {
        "source": "linkedin-profile",
        "role": latest.title,
        "roles": list(dict.fromkeys(item.title for item in sorted_positions)),
        "started_on": min((item.started_on for item in positions if item.started_on), default=""),
        "current": any(not item.finished_on for item in positions),
        "observed_at": captured_at,
    }
    if not result["current"]:
        result["finished_on"] = max(
            (item.finished_on for item in positions if item.finished_on),
            default="",
        )
    return {
        key: value
        for key, value in result.items()
        if value is not None and value != "" and value != []
    }


def _upsert_employment_relation(
    relations: list[dict[str, Any]],
    *,
    relation_type: str,
    company_id: str,
    properties: dict[str, Any],
    source_file: str,
) -> bool:
    existing = next(
        (
            relation
            for relation in relations
            if isinstance(relation, dict)
            and relation.get("type") == relation_type
            and relation.get("target") == company_id
            and relation.get("direction", "outgoing") == "outgoing"
        ),
        None,
    )
    if existing is None:
        relations.append(
            {
                "type": relation_type,
                "target": company_id,
                "properties": properties,
                "source": source_file,
            }
        )
        return True

    old = copy.deepcopy(existing)
    old_props = existing.get("properties") or {}
    if not isinstance(old_props, dict):
        old_props = {}
    incoming_date = str(properties.get("observed_at") or "")
    existing_date = str(old_props.get("observed_at") or "")
    if relation_type == "works_at" and existing_date and incoming_date < existing_date:
        return False
    if (
        relation_type == "works_at"
        and existing_date
        and incoming_date == existing_date
        and old_props.get("source") == "linkedin-connections"
        and normalize_name(str(old_props.get("role") or ""))
        != normalize_name(str(properties.get("role") or ""))
    ):
        return False
    if relation_type == "works_at" and not existing_date:
        old_role = str(old_props.get("role") or "")
        incoming_role = str(properties.get("role") or "")
        if old_role and normalize_name(old_role) != normalize_name(incoming_role):
            return False
    merged = dict(old_props)
    if relation_type == "former_employee_of":
        roles = [
            *([str(item) for item in old_props.get("roles") or []]),
            *([str(item) for item in properties.get("roles") or []]),
        ]
        merged.update({key: value for key, value in properties.items() if value})
        if roles:
            merged["roles"] = list(dict.fromkeys(roles))
        starts = [str(value) for value in (old_props.get("started_on"), properties.get("started_on")) if value]
        finishes = [str(value) for value in (old_props.get("finished_on"), properties.get("finished_on")) if value]
        if starts:
            merged["started_on"] = min(starts)
        if finishes:
            merged["finished_on"] = max(finishes)
    else:
        merged.update(properties)
    existing["properties"] = merged
    existing["source"] = source_file
    return existing != old


def _current_position(snapshot: LinkedInProfileSnapshot) -> ProfilePosition | None:
    current = [position for position in snapshot.positions if not position.finished_on]
    if not current:
        return None
    return sorted(current, key=lambda item: (item.started_on, item.title), reverse=True)[0]


def _should_replace_current_role(
    properties: dict[str, Any],
    *,
    company: str,
    role: str,
    captured_at: str,
) -> tuple[bool, str | None]:
    evidence = properties.get("current_role_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    existing_date = str(evidence.get("observed_at") or "")
    existing_company = str(properties.get("current_company") or "")
    existing_role = str(properties.get("current_role") or "")
    if existing_date:
        if captured_at > existing_date:
            return True, None
        if captured_at == existing_date:
            if (
                evidence.get("source") == "linkedin-connections"
                and (
                    normalize_name(existing_company) != normalize_name(company)
                    or normalize_name(existing_role) != normalize_name(role)
                )
            ):
                return (
                    False,
                    "Profile capture has the same date as conflicting LinkedIn Connections "
                    "evidence; the Connections role was retained by source precedence.",
                )
            return True, None
        return (
            False,
            f"Profile capture {captured_at} is older than current-role evidence "
            f"{existing_date}; snapshot was retained without overwriting the existing role.",
        )
    if not existing_company and not existing_role:
        return True, None
    if normalize_name(existing_company) == normalize_name(company) and normalize_name(
        existing_role
    ) == normalize_name(role):
        return True, None
    return True, None


def _record_review_items(
    root: Path,
    *,
    person_id: str,
    captured_at: str,
    source_file: str,
    warnings: list[str],
    excerpt: str,
) -> Path | None:
    if not warnings:
        return None
    path = root / "inbox" / "enrichment" / "review-items.yaml"
    data = _load_yaml_map(path)
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    key = f"{person_id}:{captured_at}:{source_file}"
    item = {
        "key": key,
        "kind": "linkedin-profile-parse",
        "person_id": person_id,
        "captured_at": captured_at,
        "source_file": source_file,
        "warnings": list(dict.fromkeys(warnings)),
        "evidence_excerpt": excerpt[:1000],
        "state": "pending",
    }
    existing_index = next(
        (index for index, candidate in enumerate(items) if candidate.get("key") == key),
        None,
    )
    if existing_index is None:
        items.append(item)
    else:
        old_state = items[existing_index].get("state", "pending")
        item["state"] = old_state
        items[existing_index] = item
    data = {"version": 1, "items": sorted(items, key=lambda candidate: str(candidate.get("key")))}
    _write_yaml_if_changed(path, data)
    return path


def _update_queue_after_import(
    root: Path,
    *,
    queue: str | Path | None,
    person_id: str,
    captured_at: str,
    source_file: str,
    warnings: list[str],
) -> None:
    path = _queue_path(root, queue)
    if not path.exists():
        return
    data = _load_yaml_map(path)
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    item = next((candidate for candidate in items if str(candidate.get("person_id")) == person_id), None)
    if item is None:
        return
    item["state"] = "needs-review" if warnings else "imported"
    item["captured_at"] = captured_at
    item["local_source_filename"] = source_file
    item["imported_at"] = captured_at
    if warnings:
        item["parse_warnings"] = list(dict.fromkeys(warnings))
    else:
        item.pop("parse_warnings", None)
    _write_yaml_if_changed(path, data)


def import_linkedin_profile_pdf(
    source: str | Path,
    *,
    person_id: str,
    captured_at: str,
    vault: str | Path | None = None,
    queue: str | Path | None = None,
) -> dict[str, Any]:
    """Import one profile PDF against an explicitly supplied person entity ID."""
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "import_linkedin_profile_pdf")

    root = resolve_vault(vault)
    source_path = Path(source).resolve()
    _require_capture_date(captured_at)
    person_path = _entity_path_by_id(root, person_id)
    if person_path is None:
        raise ValueError(f"No person entity exists with id {person_id!r}")
    person_metadata, person_body = read_frontmatter(person_path)
    if person_metadata.get("type") != "person":
        raise ValueError(f"Entity {person_id!r} is not a person")

    extracted = extract_pdf(source_path)
    if extracted.is_empty:
        detail = "; ".join(extracted.warnings) or "PDF contained no extractable text"
        raise ValueError(detail)
    snapshot = parse_linkedin_profile_text(
        extracted.text,
        expected_name=str(person_metadata.get("name") or ""),
    )
    existing_properties = person_metadata.get("properties") or {}
    if not isinstance(existing_properties, dict):
        existing_properties = {}
    existing_evidence = existing_properties.get("current_role_evidence") or {}
    if not isinstance(existing_evidence, dict):
        existing_evidence = {}
    existing_current_date = str(existing_evidence.get("observed_at") or "")
    existing_current_company = str(existing_properties.get("current_company") or "")
    existing_current_role = str(existing_properties.get("current_role") or "")

    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    processed_path = (
        root
        / "inbox"
        / "processed"
        / "linkedin-profiles"
        / f"{person_id}-{captured_at}-{digest[:12]}.pdf"
    )
    _copy_if_needed(source_path, processed_path)
    source_file = processed_path.relative_to(root).as_posix()

    company_index = _entity_index(root)
    position_groups: dict[tuple[str, str], list[ProfilePosition]] = {}
    for position in snapshot.positions:
        display, aliases = _resolve_company_display(position.company)
        company_path = company_index.get(_company_key(display))
        company_id, company_path, _ = _read_or_create_entity(
            root,
            entity_type="company",
            name=display,
            source_file=source_file,
            extractor=PROFILE_IMPORTER,
            properties={"source": ["linkedin-profile"]},
            tags=["linkedin", "employment"],
            body_append=(
                _company_body(display, str(person_metadata.get("name") or "vault owner"))
                if company_path is None
                else ""
            ),
            existing_path=company_path,
            aliases=[position.company, *aliases],
            preserve_existing_authority=True,
        )
        company_index.setdefault(_company_key(display), company_path)
        relation_type = "works_at" if not position.finished_on else "former_employee_of"
        if (
            relation_type == "works_at"
            and existing_current_date
            and captured_at < existing_current_date
            and (
                normalize_name(existing_current_company) != normalize_name(display)
                or normalize_name(existing_current_role) != normalize_name(position.title)
            )
        ):
            relation_type = "former_employee_of"
        position_groups.setdefault((relation_type, company_id), []).append(
            ProfilePosition(
                company=display,
                title=position.title,
                started_on=position.started_on,
                finished_on=position.finished_on,
                location=position.location,
                description=position.description,
            )
        )

    before_metadata = copy.deepcopy(person_metadata)
    before_body = person_body
    warnings = list(snapshot.warnings)
    properties = person_metadata.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    properties = _merge_properties(
        properties,
        {
            "headline": snapshot.headline,
            "location": snapshot.location,
            "source": ["linkedin-profile"],
        },
    )
    person_metadata["properties"] = properties
    person_metadata["review_status"] = "proposed"
    person_metadata["tags"] = _merge_tags(person_metadata.get("tags"), ["linkedin", "profile-enriched"])
    person_metadata["aliases"] = _merge_aliases(
        person_metadata.get("aliases"),
        [],
        str(person_metadata.get("name") or ""),
    )
    relations = person_metadata.setdefault("relations", [])
    if not isinstance(relations, list):
        relations = []
        person_metadata["relations"] = relations

    current_company_ids = {
        company_id
        for (relation_type, company_id) in position_groups
        if relation_type == "works_at"
    }
    ended_company_ids = {
        company_id
        for (relation_type, company_id) in position_groups
        if relation_type == "former_employee_of"
    } - current_company_ids
    if ended_company_ids:
        relations[:] = [
            relation
            for relation in relations
            if not (
                isinstance(relation, dict)
                and relation.get("type") in ("works_at", "recruits_for")
                and relation.get("target") in ended_company_ids
                and relation.get("source") == "Connections.csv"
            )
        ]

    for (relation_type, company_id), positions in sorted(position_groups.items()):
        relation_properties = _position_relation_properties(
            positions,
            captured_at=captured_at,
        )
        if relation_type == "former_employee_of":
            relation_properties["current"] = False
        _upsert_employment_relation(
            relations,
            relation_type=relation_type,
            company_id=company_id,
            properties=relation_properties,
            source_file=source_file,
        )

    current = _current_position(snapshot)
    if current:
        current_company, _ = _resolve_company_display(current.company)
        replace, warning = _should_replace_current_role(
            properties,
            company=current_company,
            role=current.title,
            captured_at=captured_at,
        )
        if replace:
            properties["current_company"] = current_company
            properties["current_role"] = current.title
            if current.started_on:
                properties["current_position_started_on"] = current.started_on
            properties["current_role_evidence"] = {
                "source": "linkedin-profile",
                "observed_at": captured_at,
                "source_file": source_file,
            }
        elif warning:
            warnings.append(warning)
    elif snapshot.positions:
        former_companies = {
            normalize_name(_resolve_company_display(position.company)[0])
            for position in snapshot.positions
            if position.finished_on
        }
        if normalize_name(str(properties.get("current_company") or "")) in former_companies:
            for key in (
                "current_company",
                "current_role",
                "current_position_started_on",
                "current_role_evidence",
            ):
                properties.pop(key, None)

    existing_snapshot_at = str(properties.get("linkedin_profile_snapshot_at") or "")
    existing_snapshot_hash = str(properties.get("linkedin_profile_source_sha256") or "")
    snapshot_wins = (
        not existing_snapshot_at
        or captured_at > existing_snapshot_at
        or (captured_at == existing_snapshot_at and digest >= existing_snapshot_hash)
    )
    if snapshot_wins:
        properties["linkedin_profile_snapshot_at"] = captured_at
        properties["linkedin_profile_source_file"] = source_file
        properties["linkedin_profile_source_sha256"] = digest
        snapshot_content = _snapshot_markdown(
            snapshot,
            captured_at=captured_at,
            source_file=source_file,
        )
        person_body = _managed_section(
            person_body,
            "linkedin-profile-snapshot",
            snapshot_content,
        )

    candidate_provenance = dict(person_metadata.get("provenance") or {})
    if snapshot_wins:
        candidate_provenance.update(
            {
                "source_file": source_file,
                "extracted_by": PROFILE_IMPORTER,
                "captured_at": captured_at,
            }
        )
    person_metadata["provenance"] = candidate_provenance
    comparable_before = copy.deepcopy(before_metadata)
    comparable_after = copy.deepcopy(person_metadata)
    for metadata in (comparable_before, comparable_after):
        metadata.pop("updated_at", None)
        provenance = metadata.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("extracted_at", None)
    if comparable_after != comparable_before or person_body.strip() != before_body.strip():
        now = utc_now()
        person_metadata["updated_at"] = now
        person_metadata["provenance"] = {**candidate_provenance, "extracted_at": now}
        write_frontmatter(person_path, person_metadata, person_body)

    review_path = _record_review_items(
        root,
        person_id=person_id,
        captured_at=captured_at,
        source_file=source_file,
        warnings=warnings,
        excerpt=snapshot.raw_text,
    )
    _update_queue_after_import(
        root,
        queue=queue,
        person_id=person_id,
        captured_at=captured_at,
        source_file=source_file,
        warnings=warnings,
    )
    reindex(root, full=True)
    return {
        "person_id": person_id,
        "person_file": str(person_path.relative_to(root)),
        "source_file": source_file,
        "captured_at": captured_at,
        "positions": len(snapshot.positions),
        "education": len(snapshot.education),
        "certifications": len(snapshot.certifications),
        "skills": len(snapshot.skills),
        "warnings": list(dict.fromkeys(warnings)),
        "review_items": str(review_path) if review_path else None,
    }
