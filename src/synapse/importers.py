"""Deterministic structured-data importers."""

from __future__ import annotations

import copy
import csv
import datetime as dt
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.config import load_config, resolve_vault
from synapse.index import reindex
from synapse.models import ENTITY_TYPE_FOLDERS
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


def _agent_guide_reminder() -> None:
    print("Tip: Re-run `synapse agent-guide` after large imports to refresh the vault guide.", file=sys.stderr)

LINKEDIN_IMPORTER = "deterministic:linkedin-connections"
LINKEDIN_CERTIFICATIONS_IMPORTER = "deterministic:linkedin-certifications"
LINKEDIN_POSITIONS_IMPORTER = "deterministic:linkedin-positions"
LINKEDIN_EDUCATION_IMPORTER = "deterministic:linkedin-education"
LINKEDIN_SKILLS_IMPORTER = "deterministic:linkedin-skills"
LINKEDIN_RECOMMENDATIONS_RECEIVED_IMPORTER = "deterministic:linkedin-recommendations-received"
LINKEDIN_RECOMMENDATIONS_GIVEN_IMPORTER = "deterministic:linkedin-recommendations-given"
LINKEDIN_SAVED_JOBS_IMPORTER = "deterministic:linkedin-saved-jobs"
LINKEDIN_JOB_APPLICATIONS_IMPORTER = "deterministic:linkedin-job-applications"
LINKEDIN_INVITATIONS_IMPORTER = "deterministic:linkedin-invitations"
LINKEDIN_MESSAGES_IMPORTER = "deterministic:linkedin-messages"
LINKEDIN_ENDORSEMENTS_RECEIVED_IMPORTER = "deterministic:linkedin-endorsements-received"
LINKEDIN_ENDORSEMENTS_GIVEN_IMPORTER = "deterministic:linkedin-endorsements-given"
LINKEDIN_EVENTS_IMPORTER = "deterministic:linkedin-events"


# Substrings that specifically indicate a recruiting role. Deliberately narrow:
# bare "hr"/"people"/"talent"/"sourcing"/"hiring"/"acquisition" were too noisy
# (they matched HR generalists, People Ops, supply-chain sourcing, etc.).
RECRUITER_PATTERNS = (
    "recruit",  # recruiter / recruiting / recruitment / recruits
    "talent acquisition",
    "talent partner",
    "talent sourc",
    "headhunt",
    "sourcer",
    "staffing",
)


@dataclass(frozen=True)
class LinkedInEducation:
    school_name: str
    started_on: str = ""
    finished_on: str = ""
    notes: str = ""
    degree_name: str = ""
    activities: str = ""


@dataclass(frozen=True)
class LinkedInSkill:
    name: str


@dataclass(frozen=True)
class LinkedInRecommendation:
    first_name: str
    last_name: str
    company: str
    job_title: str
    text: str
    creation_date: str
    status: str = ""


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


@dataclass(frozen=True)
class LinkedInCertification:
    name: str
    url: str = ""
    authority: str = ""
    started_on: str = ""
    finished_on: str = ""
    license_number: str = ""


@dataclass(frozen=True)
class LinkedInPosition:
    company_name: str
    title: str
    description: str = ""
    location: str = ""
    started_on: str = ""
    finished_on: str = ""


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


def _field_index_for_aliases(header: list[str], aliases_by_field: dict[str, set[str]]) -> dict[str, int]:
    by_key = {_header_key(value): index for index, value in enumerate(header)}
    indexes: dict[str, int] = {}
    for field, aliases in aliases_by_field.items():
        for alias in aliases:
            key = _header_key(alias)
            if key in by_key:
                indexes[field] = by_key[key]
                break
    return indexes


def _field_index(header: list[str]) -> dict[str, int]:
    return _field_index_for_aliases(header, _FIELD_ALIASES)


def _cell(row: list[str], indexes: dict[str, int], field: str) -> str:
    index = indexes.get(field)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


_LINKEDIN_TEXT_REPLACEMENTS = {
    "\u00e2\u20ac\u00a2": "\u2022",
    "\u00e2\u20ac\u201c": "-",
    "\u00e2\u20ac\u201d": "-",
    "\u00e2\u20ac\u2122": "'",
    "\u00e2\u20ac\u0153": '"',
    "\u00e2\u20ac\u009d": '"',
    "\u00c2": "",
}


def _clean_linkedin_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    for broken, fixed in _LINKEDIN_TEXT_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


_CONNECTED_ON_INPUT_FORMATS = ("%d %b %Y", "%d %B %Y")
_MONTH_YEAR_INPUT_FORMATS = ("%b %Y", "%B %Y")


def _normalize_connected_on(value: str) -> str:
    """Normalize LinkedIn connection dates to ISO (YYYY-MM-DD).

    LinkedIn exports dates like "25 May 2026". Parse those to ISO so dates
    sort and filter correctly. Any value that does not match a known format
    (including dates already in ISO form) is returned unchanged.
    """

    raw = value.strip()
    if not raw:
        return ""
    for fmt in _CONNECTED_ON_INPUT_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _normalize_month_year(value: str) -> str:
    """Normalize LinkedIn month-year values to YYYY-MM while preserving precision."""

    raw = value.strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw
    for fmt in _MONTH_YEAR_INPUT_FORMATS:
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return f"{parsed.year:04d}-{parsed.month:02d}"
        except ValueError:
            continue
    return raw


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
            connected_on=_normalize_connected_on(_cell(row, indexes, "connected_on")),
        )
        duplicate_key = (normalize_name(connection.name), normalize_name(connection.profile_url))
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate for {connection.name}.")
            continue
        seen.add(duplicate_key)
        connections.append(connection)
    return connections, warnings


_CERTIFICATION_FIELD_ALIASES = {
    "name": {"name", "certification", "certificationname"},
    "url": {"url", "credentialurl", "certificateurl"},
    "authority": {"authority", "issuingorganization", "issuer"},
    "started_on": {"startedon", "startdate", "issuedate"},
    "finished_on": {"finishedon", "expirationdate", "expireson"},
    "license_number": {"licensenumber", "credentialid", "licenseid"},
}


def parse_linkedin_certifications_csv(
    path: str | Path,
) -> tuple[list[LinkedInCertification], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    header: list[str] = []
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if "name" in keys and ("authority" in keys or "licensenumber" in keys or "url" in keys):
            header_index = index
            header = row
            break
    if header_index == -1:
        raise ValueError("LinkedIn certifications CSV requires a Name column.")

    indexes = _field_index_for_aliases(header, _CERTIFICATION_FIELD_ALIASES)
    if indexes.get("name") is None:
        raise ValueError("LinkedIn certifications CSV requires a Name column.")

    certifications: list[LinkedInCertification] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(
                f"Row {row_number} had fewer columns than the header; missing fields were treated as blank."
            )
        name = _cell(row, indexes, "name")
        if not name:
            warnings.append(f"Row {row_number} skipped because it had no certification name.")
            continue
        certification = LinkedInCertification(
            name=name,
            url=_cell(row, indexes, "url"),
            authority=_cell(row, indexes, "authority"),
            started_on=_normalize_month_year(_cell(row, indexes, "started_on")),
            finished_on=_normalize_month_year(_cell(row, indexes, "finished_on")),
            license_number=_cell(row, indexes, "license_number"),
        )
        duplicate_key = (
            normalize_name(certification.name),
            normalize_name(certification.authority),
            normalize_name(certification.license_number or certification.url),
        )
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate for {certification.name}.")
            continue
        seen.add(duplicate_key)
        certifications.append(certification)
    return certifications, warnings


_POSITION_FIELD_ALIASES = {
    "company_name": {"companyname", "company", "organization", "organisation", "employer"},
    "title": {"title", "position", "role", "jobtitle"},
    "description": {"description", "summary"},
    "location": {"location"},
    "started_on": {"startedon", "startdate", "from"},
    "finished_on": {"finishedon", "enddate", "to"},
}


def parse_linkedin_positions_csv(path: str | Path) -> tuple[list[LinkedInPosition], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    header: list[str] = []
    required = {"companyname", "title"}
    optional = {"description", "location", "startedon", "finishedon"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) and keys & optional:
            header_index = index
            header = row
            break
    if header_index == -1:
        raise ValueError("LinkedIn positions CSV requires Company Name and Title columns.")

    indexes = _field_index_for_aliases(header, _POSITION_FIELD_ALIASES)
    if indexes.get("company_name") is None or indexes.get("title") is None:
        raise ValueError("LinkedIn positions CSV requires Company Name and Title columns.")

    positions: list[LinkedInPosition] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(
                f"Row {row_number} had fewer columns than the header; missing fields were treated as blank."
            )

        company_name = _clean_linkedin_text(_cell(row, indexes, "company_name"))
        title = _clean_linkedin_text(_cell(row, indexes, "title"))
        if not company_name or not title:
            warnings.append(f"Row {row_number} skipped because it had no company or title.")
            continue

        position = LinkedInPosition(
            company_name=company_name,
            title=title,
            description=_clean_linkedin_text(_cell(row, indexes, "description")),
            location=_clean_linkedin_text(_cell(row, indexes, "location")),
            started_on=_normalize_month_year(_cell(row, indexes, "started_on")),
            finished_on=_normalize_month_year(_cell(row, indexes, "finished_on")),
        )
        duplicate_key = (
            normalize_name(position.company_name),
            normalize_name(position.title),
            position.started_on,
            position.finished_on,
        )
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate for {position.title}.")
            continue
        seen.add(duplicate_key)
        positions.append(position)
    return positions, warnings


def _normalize_url(value: str) -> str:
    """Normalize a LinkedIn profile URL for stable identity comparison.

    Canonicalizes away the scheme and a leading ``www.`` so the same profile
    matches across LinkedIn exports that write it differently. Connections.csv
    uses ``https://www.linkedin.com/in/x`` while Endorsements/other exports use
    a bare ``www.linkedin.com/in/x``; both must resolve to one person.
    """

    text = value.strip().casefold().rstrip("/")
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    return text


def _source_observed_at(path: Path) -> str:
    """Use the source file timestamp as deterministic snapshot evidence."""

    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC).date().isoformat()


def _name_key(entity_type: str, name: str) -> tuple[str, str]:
    return (entity_type, f"name:{normalize_name(name)}")


def _url_key(url: str) -> tuple[str, str]:
    return ("person", f"url:{url}")


def _entity_index(vault: Path) -> dict[tuple[str, str], Path]:
    """Index existing entities for identity resolution.

    People with a LinkedIn URL are indexed by URL as well as by name/aliases
    so they can be resolved by URL when present, or by name as a fallback.
    """

    results: dict[tuple[str, str], Path] = {}
    for path in entity_files(vault):
        metadata, _ = read_frontmatter(path)
        entity_type = str(metadata.get("type") or "")
        properties = metadata.get("properties")
        url = ""
        if isinstance(properties, dict):
            url = _normalize_url(str(properties.get("linkedin_url") or ""))
        if entity_type == "person" and url:
            results.setdefault(_url_key(url), path)
        if entity_type == "conversation" and isinstance(properties, dict):
            conv_id = properties.get("conversation_id") or ""
            if conv_id:
                results[("conversation", f"id:{conv_id}")] = path
        labels = [str(metadata.get("name") or ""), *[str(item) for item in metadata.get("aliases") or []]]
        for label in labels:
            if not normalize_name(label):
                continue
            if entity_type == "company":
                results.setdefault(_company_key(label), path)
            else:
                results.setdefault(_name_key(entity_type, label), path)
    return results


def _resolve_person(index: dict[tuple[str, str], Path], name: str, url: str) -> Path | None:
    """Find the existing person entity for an incoming connection.

    A non-empty URL is the strongest identity. When a URL is provided, we only
    match a URL-less entity (to associate the URL with it) or an exact URL match.
    If no URL is provided, we fall back to name-based resolution.
    """

    if url:
        hit = index.get(_url_key(url))
        if hit is not None:
            return hit
        # Fallback to name ONLY if the existing person has no URL
        name_hit = index.get(_name_key("person", name))
        if name_hit is not None:
            metadata, _ = read_frontmatter(name_hit)
            existing_url = ""
            properties = metadata.get("properties")
            if isinstance(properties, dict):
                existing_url = properties.get("linkedin_url") or ""
            if not existing_url:
                return name_hit
        return None
    return index.get(_name_key("person", name))


def _entity_dir(vault: Path, entity_type: str) -> Path:
    folder = ENTITY_TYPE_FOLDERS.get(entity_type, entity_type)
    return vault / "entities" / folder


def _merge_properties(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "source":
            existing_sources = merged.get("source") or []
            if isinstance(existing_sources, str):
                existing_sources = [existing_sources]
            incoming_sources = value if isinstance(value, list) else [value]
            merged["source"] = list(
                dict.fromkeys(
                    str(item) for item in [*existing_sources, *incoming_sources] if item
                )
            )
        elif key in ("emails", "phones"):
            # List-append-dedupe semantics: never overwrite, preserve order,
            # casefold-dedupe for emails, digits-only-dedupe for phones.
            existing_items = merged.get(key) or []
            if isinstance(existing_items, str):
                existing_items = [existing_items]
            incoming_items = value if isinstance(value, list) else ([value] if value else [])
            if key == "emails":
                # casefold-dedupe
                seen: set[str] = {str(i).casefold() for i in existing_items if i}
                combined: list[str] = list(existing_items)
                for item in incoming_items:
                    if item and str(item).casefold() not in seen:
                        seen.add(str(item).casefold())
                        combined.append(str(item))
                merged[key] = combined
            else:
                # phones: digits-only-dedupe
                import re as _re
                seen_digits: set[str] = {_re.sub(r"[^\d]", "", str(i)) for i in existing_items if i}
                combined_p: list[str] = list(existing_items)
                for item in incoming_items:
                    if item:
                        digits = _re.sub(r"[^\d]", "", str(item))
                        if digits and digits not in seen_digits:
                            seen_digits.add(digits)
                            combined_p.append(str(item))
                merged[key] = combined_p
        elif value is not None and value != "" and value != [] and not merged.get(key):
            merged[key] = value
    return merged


def _merge_tags(existing: Any, incoming: list[str]) -> list[str]:
    tags = existing if isinstance(existing, list) else []
    return list(dict.fromkeys([*map(str, tags), *incoming]))


def is_recruiter_title(title: str) -> bool:
    title_lower = title.casefold()
    
    # Remove generic matches first
    negatives = ["founder", "ceo", "engineer", "scientist"]
    if any(neg in title_lower for neg in negatives):
        return False
        
    # Check positive tokens
    positives = ["recruit", "talent", "sourcing", "staffing", "people ops", "people operations", "headhunt"]
    if any(pos in title_lower for pos in positives):
        return True
        
    # HR requires word boundary
    if re.search(r"\bhr\b", title_lower):
        return True
        
    return False


def _is_likely_recruiter(connection: LinkedInConnection) -> bool:
    return is_recruiter_title(connection.position)


# Company-name strings that are not real shared employers. Treating them as
# entities would create misleading hubs linking unrelated people, so we keep
# the raw string on the person but do not create a company node or relation.
_PLACEHOLDER_COMPANIES = {
    "self-employed",
    "self employed",
    "selfemployed",
    "self",
    "freelance",
    "freelancer",
    "freelancing",
    "independent",
    "unemployed",
    "none",
    "n/a",
    "na",
    "--",
    "-",
    "student",
    "open to work",
    "looking for opportunities",
    "private",
    "nda",  # "under NDA" — the person cannot disclose their employer
    "n.d.a",
}

# Legal-entity tokens stripped when deriving a company's identity, so that
# "Google" and "Google LLC" resolve to the same canonical company.
_LEGAL_SUFFIX_TOKENS = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "pvt",
    "private",
    "corp",
    "corporation",
    "co",
    "company",
    "gmbh",
    "ag",
    "sa",
    "plc",
    "llp",
    "lp",
    "pte",
    "bv",
    "oy",
    "srl",
    "kg",
}


def _is_placeholder_company(name: str) -> bool:
    normalized = normalize_name(name)
    if not normalized:
        return True
    if normalized in _PLACEHOLDER_COMPANIES:
        return True
    return normalized.startswith(("stealth", "confidential", "self-employed", "self employed"))


def _is_non_entity_company_for_connection(connection: LinkedInConnection) -> bool:
    """Return true when a company string should stay as raw person context."""

    if _is_placeholder_company(connection.company):
        return True
    return normalize_name(connection.company) == normalize_name(connection.name)


def _canonical_company(name: str) -> str:
    """Derive a stable identity key for a company display name.

    Punctuation is dropped and legal-entity tokens are removed so common
    spelling variants collapse together. Falls back to the normalized name
    when stripping would leave nothing.
    """

    stripped = re.sub(r"[^\w\s]", " ", name.casefold())
    tokens = [token for token in stripped.split() if token and token not in _LEGAL_SUFFIX_TOKENS]
    return " ".join(tokens) or normalize_name(name)


def _company_key(name: str) -> tuple[str, str]:
    return ("company", f"canon:{_canonical_company(name)}")


_FORMER_NAME_RE = re.compile(r"\(\s*(?:formerly|previously|fka|f/k/a|née|nee)\s+([^)]+)\)", re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)")


def _resolve_company_display(raw: str) -> tuple[str, list[str]]:
    """Map a raw LinkedIn company string to its canonical display name.

    Returns the display name plus any extra aliases worth recording (e.g. a
    former name parsed from a "(formerly X)" suffix). Parenthetical badges and
    former-name notes are stripped generically. Ambiguous employer names remain
    distinct until an explicit merge is reviewed.
    """

    aliases: list[str] = []
    former = _FORMER_NAME_RE.search(raw)
    if former:
        aliases.append(former.group(1).strip())

    cleaned = _PARENTHETICAL_RE.sub("", raw).strip() or raw
    return cleaned, aliases


def _merge_aliases(existing: Any, incoming: list[str], name: str) -> list[str]:
    aliases = existing if isinstance(existing, list) else []
    canonical_name = normalize_name(name)
    merged = list(dict.fromkeys(str(item) for item in [*aliases, *incoming] if item))
    return [alias for alias in merged if normalize_name(alias) != canonical_name]


def _wikilink(slug: str, label: str) -> str:
    """An Obsidian-style wikilink to an entity's file, displayed as ``label``."""

    return f"[[{slug}|{label}]]"


def _person_body(
    *,
    owner_name: str,
    owner_id: str,
    position: str,
    company_display: str,
    company_slug: str,
    profile_url: str,
    connected_on: str,
    email: str,
) -> str:
    # Wikilinks make the relationships visible in Obsidian's graph (which only
    # renders [[links]], not structured frontmatter relations) and feed the
    # weak-link index. The owner note is entities/people/me.md (slug "me").
    owner_ref = _wikilink("me", owner_name) if owner_id == "me" else owner_name
    company_ref = _wikilink(company_slug, company_display) if company_display and company_slug else company_display

    if position and company_ref:
        role_line = f"{position} at {company_ref}"
    elif position:
        role_line = position
    elif company_ref:
        role_line = f"Works at {company_ref}"
    else:
        role_line = "Role not listed."

    lines = [f"LinkedIn connection of {owner_ref}.", "", f"- **Role:** {role_line}"]
    if connected_on:
        lines.append(f"- **Connected on:** {connected_on}")
    if profile_url:
        lines.append(f"- **LinkedIn:** {profile_url}")
    if email:
        lines.append(f"- **Email:** {email}")
    return "\n".join(lines)


def _company_body(name: str, owner_name: str) -> str:
    return f"{name} appears in {owner_name}'s LinkedIn network."


def _certification_body(certification: LinkedInCertification, owner_name: str, owner_id: str) -> str:
    owner_ref = _wikilink("me", owner_name) if owner_id == "me" else owner_name
    lines = [f"LinkedIn certification associated with {owner_ref}."]
    if certification.authority:
        lines.append(f"- **Authority:** {certification.authority}")
    if certification.started_on:
        lines.append(f"- **Started on:** {certification.started_on}")
    if certification.finished_on:
        lines.append(f"- **Finished on:** {certification.finished_on}")
    if certification.license_number:
        lines.append(f"- **License number:** {certification.license_number}")
    if certification.url:
        lines.append(f"- **Credential URL:** {certification.url}")
    return "\n".join(lines)


_POSITION_SKILL_PATTERNS = (
    (r"\bMPC Cryptography\b", "MPC Cryptography"),
    (r"\bthreshold cryptography\b", "Threshold Cryptography"),
    (r"\bprivate permissioned blockchain\b", "Private Permissioned Blockchain"),
    (r"\bsmart contracts?\b", "Smart Contracts"),
    (r"\bblockchain APIs?\b", "Blockchain APIs"),
    (r"\bweb3\.js\b", "web3.js"),
    (r"\bethers\.js\b", "ethers.js"),
    (r"\bAWS EKS\b", "AWS EKS"),
    (r"\bAWS RDS\b", "AWS RDS"),
    (r"\bKubernetes\b", "Kubernetes"),
    (r"\bHelm\b", "Helm"),
    (r"\bDocker\b", "Docker"),
    (r"\bSolidity\b", "Solidity"),
    (r"\bPostgreSQL\b", "PostgreSQL"),
    (r"\bGolang\b", "Golang"),
    (r"\bJavaScript\b", "JavaScript"),
    (r"\bTypeScript\b", "TypeScript"),
    (r"\bNode\.js\b", "Node.js"),
    (r"\bBLS\b", "BLS Digital Signatures"),
    (r"\bECDSA\b", "ECDSA"),
    (r"\bEDDSA\b", "EdDSA"),
)


def _position_description_items(description: str) -> list[str]:
    text = _clean_linkedin_text(description)
    if not text:
        return []
    if "\u2022" in text:
        return [item.strip(" -\n\t") for item in text.split("\u2022") if item.strip(" -\n\t")]
    return [line.strip(" -\n\t") for line in text.splitlines() if line.strip(" -\n\t")]


def _position_date_range(position: LinkedInPosition) -> str:
    start = position.started_on or "unknown"
    end = position.finished_on or "present"
    return f"{start} to {end}"


def _position_skills(position: LinkedInPosition) -> list[str]:
    haystack = f"{position.title}\n{position.description}"
    skills = [
        label
        for pattern, label in _POSITION_SKILL_PATTERNS
        if re.search(pattern, haystack, flags=re.IGNORECASE)
    ]
    return list(dict.fromkeys(skills))


def _position_record(position: LinkedInPosition, company_display: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "title": position.title,
            "company": company_display,
            "raw_company": position.company_name,
            "location": position.location,
            "started_on": position.started_on,
            "finished_on": position.finished_on,
            "current": not bool(position.finished_on),
            "description_items": _position_description_items(position.description),
            "skills": _position_skills(position),
            "source": "linkedin",
        }.items()
        if value != "" and value != []
    }


def _position_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        normalize_name(str(record.get("title") or "")),
        normalize_name(str(record.get("raw_company") or record.get("company") or "")),
        str(record.get("started_on") or ""),
        str(record.get("finished_on") or ""),
    )


def _sorted_position_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            str(item.get("started_on") or ""),
            str(item.get("finished_on") or "9999-99"),
            str(item.get("title") or ""),
        ),
        reverse=True,
    )


def _employment_relation_properties(records: list[dict[str, Any]]) -> dict[str, Any]:
    positions = _sorted_position_records(records)
    latest = positions[0] if positions else {}
    started_values = sorted(str(item.get("started_on")) for item in positions if item.get("started_on"))
    finished_values = sorted(
        str(item.get("finished_on")) for item in positions if item.get("finished_on")
    )
    current = any(bool(item.get("current")) for item in positions)
    properties: dict[str, Any] = {
        "source": "linkedin",
        "role": latest.get("title"),
        "roles": list(dict.fromkeys(str(item.get("title")) for item in positions if item.get("title"))),
        "started_on": started_values[0] if started_values else "",
        "current": current,
    }
    if not current and finished_values:
        properties["finished_on"] = finished_values[-1]
    return {key: value for key, value in properties.items() if value != "" and value != []}


def _company_position_body(
    position: LinkedInPosition,
    *,
    owner_name: str,
    owner_id: str,
) -> str:
    owner_ref = _wikilink("me", owner_name) if owner_id == "me" else owner_name
    lines = [
        f"{owner_ref} listed this company in LinkedIn work history.",
        f"- **Role:** {position.title}",
        f"- **Dates:** {_position_date_range(position)}",
    ]
    if position.location:
        lines.append(f"- **Location:** {position.location}")
    return "\n".join(lines)


def _managed_section(body: str, section_id: str, content: str) -> str:
    start = f"<!-- synapse:{section_id}:start -->"
    end = f"<!-- synapse:{section_id}:end -->"
    replacement = f"{start}\n{content.strip()}\n{end}"
    pattern = re.compile(rf"\n*{re.escape(start)}.*?{re.escape(end)}", flags=re.DOTALL)
    if pattern.search(body):
        return pattern.sub(f"\n\n{replacement}", body).strip()
    return f"{body.rstrip()}\n\n{replacement}".strip()


def _owner_positions_section(entries: list[dict[str, Any]]) -> str:
    lines = ["## LinkedIn Positions", ""]
    for entry in sorted(
        entries,
        key=lambda item: (
            str(item["position"].started_on or ""),
            str(item["position"].finished_on or "9999-99"),
            str(item["position"].title or ""),
        ),
        reverse=True,
    ):
        position: LinkedInPosition = entry["position"]
        company_ref = _wikilink(str(entry["company_slug"]), str(entry["company_display"]))
        lines.append(f"- **{position.title}**, {company_ref} ({_position_date_range(position)})")
        if position.location:
            lines.append(f"  - Location: {position.location}")
        for item in _position_description_items(position.description):
            lines.append(f"  - {item}")
        skills = _position_skills(position)
        if skills:
            lines.append(f"  - Skills: {', '.join(skills)}")
    return "\n".join(lines)


def _apply_owner_positions(
    owner_path: Path,
    *,
    entries: list[dict[str, Any]],
    source_file: str,
) -> tuple[int, int]:
    if not entries:
        return 0, 0

    metadata, body = read_frontmatter(owner_path)
    before_metadata = copy.deepcopy(metadata)
    before_body = body
    now = utc_now()
    relations = metadata.setdefault("relations", [])
    if not isinstance(relations, list):
        relations = []
        metadata["relations"] = relations

    works_at_targets: set[str] = set()
    former_targets: set[str] = set()
    records_by_relation: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        position: LinkedInPosition = entry["position"]
        relation_type = "works_at" if not position.finished_on else "former_employee_of"
        company_id = str(entry["company_id"])
        record = _position_record(position, str(entry["company_display"]))
        records_by_relation.setdefault((relation_type, company_id), []).append(record)
        if relation_type == "works_at":
            works_at_targets.add(company_id)
        else:
            former_targets.add(company_id)

    for relation_key, incoming_records in records_by_relation.items():
        relation_type, company_id = relation_key
        relation = next(
            (
                item
                for item in relations
                if isinstance(item, dict)
                and item.get("type") == relation_type
                and item.get("target") == company_id
            ),
            None,
        )
        if relation is None:
            relation = {
                "type": relation_type,
                "target": company_id,
                "properties": {},
                "source": source_file,
                "created_at": now,
            }
            relations.append(relation)
        properties = relation.setdefault("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        existing_records = [
            item for item in properties.get("positions", []) if isinstance(item, dict)
        ]
        merged_by_key = {_position_key(record): record for record in existing_records}
        for record in incoming_records:
            merged_by_key[_position_key(record)] = record
        relation["properties"] = _employment_relation_properties(list(merged_by_key.values()))
        relation["source"] = source_file

    current_entries = [
        entry
        for entry in entries
        if isinstance(entry["position"], LinkedInPosition) and not entry["position"].finished_on
    ]
    if current_entries:
        current_entries = sorted(
            current_entries,
            key=lambda item: str(item["position"].started_on or ""),
            reverse=True,
        )
        current = current_entries[0]
        current_position: LinkedInPosition = current["position"]
        metadata["properties"] = _merge_properties(
            metadata.get("properties") or {},
            {
                "current_company": current["company_display"],
                "current_role": current_position.title,
                "current_position_started_on": current_position.started_on,
                "source": ["linkedin"],
            },
        )
    else:
        metadata["properties"] = _merge_properties(
            metadata.get("properties") or {},
            {"source": ["linkedin"]},
        )

    body = _managed_section(body, "linkedin-positions", _owner_positions_section(entries))
    if metadata != before_metadata or body.strip() != before_body.strip():
        metadata["updated_at"] = now
        write_frontmatter(owner_path, metadata, body)

    return len(works_at_targets), len(former_targets)


def _read_or_create_entity(
    vault: Path,
    *,
    entity_type: str,
    name: str,
    source_file: str,
    extractor: str = LINKEDIN_IMPORTER,
    properties: dict[str, Any],
    tags: list[str],
    body_append: str,
    existing_path: Path | None = None,
    aliases: list[str] | None = None,
    preserve_existing_authority: bool = False,
) -> tuple[str, Path, bool]:
    now = utc_now()
    aliases = aliases or []
    if existing_path is not None:
        before_metadata, before_body = read_frontmatter(existing_path)
        metadata = dict(before_metadata)
        body = before_body
        if not preserve_existing_authority:
            metadata["review_status"] = "proposed"
        metadata["tags"] = _merge_tags(metadata.get("tags"), tags)
        metadata["aliases"] = _merge_aliases(metadata.get("aliases"), aliases, str(metadata.get("name") or name))
        metadata["properties"] = _merge_properties(metadata.get("properties") or {}, properties)
        # Build candidate provenance using existing timestamps so we can diff cleanly
        old_provenance = dict(metadata.get("provenance") or {})
        candidate_provenance = (
            old_provenance
            if preserve_existing_authority
            else {**old_provenance, "source_file": source_file, "extracted_by": extractor}
        )
        metadata["provenance"] = candidate_provenance
        if body_append and body_append not in body:
            body = f"{body.rstrip()}\n\n{body_append.strip()}\n"
        # Compare ignoring timestamp fields (updated_at and provenance.extracted_at).
        # All other field changes (tags, aliases, properties, review_status, source_file, extracted_by, body) count.
        def _meta_without_timestamps(m: dict) -> dict:
            out = {k: v for k, v in m.items() if k != "updated_at"}
            prov = dict(out.get("provenance") or {})
            prov.pop("extracted_at", None)
            if prov:
                out["provenance"] = prov
            elif "provenance" in out:
                del out["provenance"]
            return out
        if _meta_without_timestamps(metadata) == _meta_without_timestamps(before_metadata) and body == before_body:
            return str(metadata["id"]), existing_path, False
        # Something changed — stamp new timestamps and write
        metadata["updated_at"] = now
        metadata["provenance"] = (
            candidate_provenance
            if preserve_existing_authority
            else {**candidate_provenance, "extracted_at": now}
        )
        write_frontmatter(existing_path, metadata, body)
        return str(metadata["id"]), existing_path, False

    entity_id = generate_ulid()
    metadata = {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "aliases": _merge_aliases([], aliases, name),
        "review_status": "proposed",
        "tags": _merge_tags([], tags),
        "relations": [],
        "properties": {
            key: value
            for key, value in properties.items()
            if value is not None and value != "" and value != []
        },
        "created_at": now,
        "updated_at": now,
        "provenance": {
            "source_file": source_file,
            "extracted_by": extractor,
            "extracted_at": now,
        },
    }
    body = f"# {name}\n\n{body_append.strip()}".strip()
    path = unique_path(_entity_dir(vault, entity_type) / f"{slugify(name)}.md")
    write_frontmatter(path, metadata, body)
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
            and existing.get("direction", "outgoing") == relation.get("direction", "outgoing")
        ):
            old_properties = copy.deepcopy(existing.get("properties"))
            old_source = existing.get("source")
            incoming_properties = {
                k: v for k, v in (relation.get("properties") or {}).items() if v
            }
            if incoming_properties:
                properties = existing.get("properties")
                if not isinstance(properties, dict):
                    properties = {}
                    existing["properties"] = properties
                properties.update(incoming_properties)
            if not existing.get("source") and relation.get("source"):
                existing["source"] = relation.get("source")
            if existing.get("properties") == old_properties and existing.get("source") == old_source:
                return
            write_frontmatter(path, metadata, body)
            return
    relations.append(relation)
    write_frontmatter(path, metadata, body)


def _owner(vault: Path) -> tuple[str, str]:
    """Return the (id, display name) of the vault owner entity, if present."""

    me_path = vault / "entities" / "people" / "me.md"
    if not me_path.exists():
        return "", "you"
    metadata, _ = read_frontmatter(me_path)
    return str(metadata.get("id") or ""), str(metadata.get("name") or "you")


def import_linkedin_connections(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_connections")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    connections, warnings = parse_linkedin_connections_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()
    placeholder_companies: set[str] = set()
    knows_edges = 0
    recruits_for_edges = 0

    for connection in connections:
        company_id = ""
        company_body_display = ""
        company_slug = ""
        is_recruiter = _is_likely_recruiter(connection)
        if connection.company and not _is_non_entity_company_for_connection(connection):
            company_display, company_aliases = _resolve_company_display(connection.company)
            company_path = index.get(_company_key(company_display))
            company_id, company_path, was_created = _read_or_create_entity(
                root,
                entity_type="company",
                name=company_display,
                source_file=source_path.name,
                properties={},
                tags=["linkedin"],
                body_append=_company_body(company_display, owner_name),
                existing_path=company_path,
                aliases=[connection.company, *company_aliases],
            )
            index.setdefault(_company_key(company_display), company_path)
            company_ids.add(company_id)
            company_body_display = company_display
            company_slug = company_path.stem
            target_list = created if was_created else updated
            target_list.append({"id": company_id, "name": company_display, "file_path": str(company_path.relative_to(root))})
        elif connection.company:
            placeholder_companies.add(connection.company)
            company_body_display = connection.company  # kept as plain text, no node/link

        profile_url = connection.profile_url
        person_url = _normalize_url(profile_url)
        person_path = _resolve_person(index, connection.name, person_url)
        role_evidence: dict[str, str] | None = None
        if person_path is None:
            role_evidence = {
                "source": "linkedin-connections",
                "observed_at": _source_observed_at(source_path),
                "source_file": source_path.name,
            }
        else:
            existing_metadata, _ = read_frontmatter(person_path)
            existing_properties = existing_metadata.get("properties") or {}
            if not isinstance(existing_properties, dict):
                existing_properties = {}
            existing_company = str(existing_properties.get("current_company") or "")
            existing_role = str(existing_properties.get("current_role") or "")
            if (
                (not existing_company and not existing_role)
                or (
                    normalize_name(existing_company) == normalize_name(connection.company)
                    and normalize_name(existing_role) == normalize_name(connection.position)
                )
            ):
                role_evidence = {
                    "source": "linkedin-connections",
                    "observed_at": _source_observed_at(source_path),
                    "source_file": source_path.name,
                }
        person_id, person_path, person_created = _read_or_create_entity(
            root,
            entity_type="person",
            name=connection.name,
            source_file=source_path.name,
            properties={
                "first_name": connection.first_name,
                "last_name": connection.last_name,
                "linkedin_url": profile_url,
                "email": connection.email,
                "current_company": connection.company,
                "current_role": connection.position,
                "current_role_evidence": role_evidence,
                "connected_on": connection.connected_on,
                "source": ["linkedin"],
            },
            tags=["linkedin", *(["recruiter"] if is_recruiter else [])],
            body_append=_person_body(
                owner_name=owner_name,
                owner_id=owner_id,
                position=connection.position,
                company_display=company_body_display,
                company_slug=company_slug,
                profile_url=profile_url,
                connected_on=connection.connected_on,
                email=connection.email,
            ),
            existing_path=person_path,
        )
        name_key = _name_key("person", connection.name)
        if person_url:
            # A URL-identified person must not stay reachable by name, or a
            # different person sharing the name would later merge into them.
            index[_url_key(person_url)] = person_path
            if index.get(name_key) == person_path:
                del index[name_key]
        else:
            index.setdefault(name_key, person_path)
        target_list = created if person_created else updated
        target_list.append({"id": person_id, "name": connection.name, "file_path": str(person_path.relative_to(root))})

        if owner_id and person_id != owner_id:
            _add_relation(
                person_path,
                {
                    "type": "knows",
                    "target": owner_id,
                    "properties": {
                        key: value
                        for key, value in {
                            "connected_on": connection.connected_on,
                            "channel": "linkedin",
                        }.items()
                        if value
                    },
                    "source": source_path.name,
                },
            )
            knows_edges += 1
        if company_id:
            _add_relation(
                person_path,
                {
                    "type": "works_at",
                    "target": company_id,
                    "properties": {
                        key: value
                        for key, value in {
                            "role": connection.position,
                            "source": "linkedin-connections",
                            "observed_at": _source_observed_at(source_path),
                        }.items()
                        if value
                    },
                    "source": source_path.name,
                },
            )
            if is_recruiter:
                _add_relation(
                    person_path,
                    {"type": "recruits_for", "target": company_id, "properties": {}, "source": source_path.name},
                )
                recruits_for_edges += 1

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_IMPORTER,
        "connections": len(connections),
        "companies": len(company_ids),
        "knows_edges": knows_edges,
        "recruits_for_edges": recruits_for_edges,
        "placeholder_companies_skipped": sorted(placeholder_companies),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_certifications(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_certifications")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    certifications, warnings = parse_linkedin_certifications_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    demonstrates_skill_edges = 0

    for certification in certifications:
        skill_path = index.get(_name_key("skill", certification.name))
        properties = {
            "proficiency": "certified",
            "proof": [certification.url] if certification.url else [],
            "credential_authority": certification.authority,
            "credential_url": certification.url,
            "credential_license_number": certification.license_number,
            "credential_started_on": certification.started_on,
            "credential_finished_on": certification.finished_on,
            "source": ["linkedin"],
        }
        skill_id, skill_path, was_created = _read_or_create_entity(
            root,
            entity_type="skill",
            name=certification.name,
            source_file=source_path.name,
            extractor=LINKEDIN_CERTIFICATIONS_IMPORTER,
            properties=properties,
            tags=["linkedin", "certification"],
            body_append=_certification_body(certification, owner_name, owner_id),
            existing_path=skill_path,
        )
        index.setdefault(_name_key("skill", certification.name), skill_path)
        target_list = created if was_created else updated
        target_list.append(
            {
                "id": skill_id,
                "name": certification.name,
                "file_path": str(skill_path.relative_to(root)),
            }
        )

        if owner_id:
            _add_relation(
                root / "entities" / "people" / "me.md",
                {
                    "type": "demonstrates_skill",
                    "target": skill_id,
                    "properties": {
                        key: value
                        for key, value in {
                            "source": "linkedin",
                            "credential_name": certification.name,
                            "credential_authority": certification.authority,
                            "credential_url": certification.url,
                            "credential_license_number": certification.license_number,
                            "credential_started_on": certification.started_on,
                            "credential_finished_on": certification.finished_on,
                        }.items()
                        if value
                    },
                    "source": source_path.name,
                },
            )
            demonstrates_skill_edges += 1

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_CERTIFICATIONS_IMPORTER,
        "certifications": len(certifications),
        "demonstrates_skill_edges": demonstrates_skill_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_positions(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_positions")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    positions, warnings = parse_linkedin_positions_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn positions import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for position in positions:
        company_display, company_aliases = _resolve_company_display(position.company_name)
        company_path = index.get(_company_key(company_display))
        company_id, company_path, was_created = _read_or_create_entity(
            root,
            entity_type="company",
            name=company_display,
            source_file=source_path.name,
            extractor=LINKEDIN_POSITIONS_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin", "employment"],
            body_append=_company_position_body(
                position,
                owner_name=owner_name,
                owner_id=owner_id,
            ),
            existing_path=company_path,
            aliases=[position.company_name, *company_aliases],
        )
        index.setdefault(_company_key(company_display), company_path)
        index.setdefault(_company_key(position.company_name), company_path)
        company_ids.add(company_id)
        target_list = created if was_created else updated
        target_list.append(
            {
                "id": company_id,
                "name": company_display,
                "file_path": str(company_path.relative_to(root)),
            }
        )
        entries.append(
            {
                "position": position,
                "company_id": company_id,
                "company_display": company_display,
                "company_slug": company_path.stem,
            }
        )

    works_at_relations, former_employee_of_relations = _apply_owner_positions(
        owner_path,
        entries=entries,
        source_file=source_path.name,
    )

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_POSITIONS_IMPORTER,
        "positions": len(positions),
        "companies": len(company_ids),
        "works_at_relations": works_at_relations,
        "former_employee_of_relations": former_employee_of_relations,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Education Importer ---

_EDUCATION_FIELD_ALIASES = {
    "school_name": {"schoolname", "school"},
    "started_on": {"startdate", "startedon", "from"},
    "finished_on": {"enddate", "finishedon", "to"},
    "notes": {"notes", "description"},
    "degree_name": {"degreename", "degree"},
    "activities": {"activities", "societies"},
}


def parse_linkedin_education_csv(path: str | Path) -> tuple[list[LinkedInEducation], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    header: list[str] = []
    required = {"schoolname"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or "school" in keys or "schoolname" in keys:
            header_index = index
            header = row
            break
    if header_index == -1:
        raise ValueError("LinkedIn education CSV requires a School Name column.")

    indexes = _field_index_for_aliases(header, _EDUCATION_FIELD_ALIASES)
    if indexes.get("school_name") is None:
        raise ValueError("LinkedIn education CSV requires a School Name column.")

    education_list: list[LinkedInEducation] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(
                f"Row {row_number} had fewer columns than the header; missing fields were treated as blank."
            )

        school_name = _clean_linkedin_text(_cell(row, indexes, "school_name"))
        if not school_name:
            warnings.append(f"Row {row_number} skipped because it had no school name.")
            continue

        edu = LinkedInEducation(
            school_name=school_name,
            started_on=_normalize_month_year(_cell(row, indexes, "started_on")),
            finished_on=_normalize_month_year(_cell(row, indexes, "finished_on")),
            notes=_clean_linkedin_text(_cell(row, indexes, "notes")),
            degree_name=_clean_linkedin_text(_cell(row, indexes, "degree_name")),
            activities=_clean_linkedin_text(_cell(row, indexes, "activities")),
        )
        duplicate_key = (
            normalize_name(edu.school_name),
            normalize_name(edu.degree_name),
            edu.started_on,
        )
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate education entry.")
            continue
        seen.add(duplicate_key)
        education_list.append(edu)
    return education_list, warnings


def _education_record(edu: LinkedInEducation, school_display: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "school": school_display,
            "raw_school": edu.school_name,
            "degree": edu.degree_name,
            "started_on": edu.started_on,
            "finished_on": edu.finished_on,
            "notes": edu.notes,
            "activities": edu.activities,
            "source": "linkedin",
        }.items()
        if value != "" and value != []
    }


def _owner_education_section(entries: list[dict[str, Any]]) -> str:
    lines = ["## LinkedIn Education", ""]
    for entry in sorted(
        entries,
        key=lambda item: str(item["education"].started_on or ""),
        reverse=True,
    ):
        edu: LinkedInEducation = entry["education"]
        school_ref = _wikilink(str(entry["school_slug"]), str(entry["school_display"]))
        degree_line = f" - **{edu.degree_name}**" if edu.degree_name else ""
        date_range = f" ({edu.started_on or 'unknown'} to {edu.finished_on or 'present'})"
        lines.append(f"- {school_ref}{degree_line}{date_range}")
        if edu.activities:
            lines.append(f"  - **Activities:** {edu.activities}")
        if edu.notes:
            lines.append(f"  - {edu.notes}")
    return "\n".join(lines)


def _apply_owner_education(
    owner_path: Path,
    *,
    entries: list[dict[str, Any]],
    source_file: str,
) -> int:
    if not entries:
        return 0

    metadata, body = read_frontmatter(owner_path)
    before_metadata = copy.deepcopy(metadata)
    before_body = body
    now = utc_now()
    relations = metadata.setdefault("relations", [])
    if not isinstance(relations, list):
        relations = []
        metadata["relations"] = relations

    for entry in entries:
        edu: LinkedInEducation = entry["education"]
        school_id = str(entry["school_id"])
        record = _education_record(edu, str(entry["school_display"]))
        
        relation = next(
            (
                item
                for item in relations
                if isinstance(item, dict)
                and item.get("type") == "attended"
                and item.get("target") == school_id
            ),
            None,
        )
        if relation is None:
            relation = {
                "type": "attended",
                "target": school_id,
                "properties": {},
                "source": source_file,
                "created_at": now,
            }
            relations.append(relation)
        
        props = relation.setdefault("properties", {})
        if not isinstance(props, dict):
            props = {}
        props.update(record)
        relation["source"] = source_file

    body = _managed_section(body, "linkedin-education", _owner_education_section(entries))
    if metadata != before_metadata or body.strip() != before_body.strip():
        metadata["updated_at"] = now
        write_frontmatter(owner_path, metadata, body)

    return len(entries)


def import_linkedin_education(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_education")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    education_list, warnings = parse_linkedin_education_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn education import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    school_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for edu in education_list:
        school_display, school_aliases = _resolve_company_display(edu.school_name)
        school_path = index.get(_company_key(school_display))
        school_id, school_path, was_created = _read_or_create_entity(
            root,
            entity_type="company",
            name=school_display,
            source_file=source_path.name,
            extractor=LINKEDIN_EDUCATION_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin", "education"],
            body_append=f"{school_display} listed as educational institution in {owner_name}'s LinkedIn network.",
            existing_path=school_path,
            aliases=[edu.school_name, *school_aliases],
        )
        index.setdefault(_company_key(school_display), school_path)
        index.setdefault(_company_key(edu.school_name), school_path)
        school_ids.add(school_id)
        target_list = created if was_created else updated
        target_list.append(
            {
                "id": school_id,
                "name": school_display,
                "file_path": str(school_path.relative_to(root)),
            }
        )
        entries.append(
            {
                "education": edu,
                "school_id": school_id,
                "school_display": school_display,
                "school_slug": school_path.stem,
            }
        )

    attended_relations = _apply_owner_education(
        owner_path,
        entries=entries,
        source_file=source_path.name,
    )

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_EDUCATION_IMPORTER,
        "education_entries": len(education_list),
        "schools": len(school_ids),
        "attended_relations": attended_relations,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Skills Importer ---

def parse_linkedin_skills_csv(path: str | Path) -> tuple[list[LinkedInSkill], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    for index, row in enumerate(rows[:5]):
        keys = {_header_key(cell) for cell in row}
        if "name" in keys or "skill" in keys:
            header_index = index
            break

    skills: list[LinkedInSkill] = []
    seen: set[str] = set()
    start_row = header_index + 1 if header_index != -1 else 0
    for row_number, row in enumerate(rows[start_row:], start=start_row + 1):
        if not row:
            continue
        name = _clean_linkedin_text(row[0])
        if not name:
            continue
        normalized = normalize_name(name)
        if normalized in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate skill: {name}.")
            continue
        seen.add(normalized)
        skills.append(LinkedInSkill(name=name))
    return skills, warnings


def import_linkedin_skills(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_skills")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    skills, warnings = parse_linkedin_skills_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    demonstrates_skill_edges = 0

    for skill in skills:
        skill_path = index.get(_name_key("skill", skill.name))
        skill_id, skill_path, was_created = _read_or_create_entity(
            root,
            entity_type="skill",
            name=skill.name,
            source_file=source_path.name,
            extractor=LINKEDIN_SKILLS_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin", "skill"],
            body_append=f"LinkedIn skill associated with {owner_name}.",
            existing_path=skill_path,
        )
        index.setdefault(_name_key("skill", skill.name), skill_path)
        target_list = created if was_created else updated
        target_list.append(
            {
                "id": skill_id,
                "name": skill.name,
                "file_path": str(skill_path.relative_to(root)),
            }
        )

        if owner_id:
            _add_relation(
                root / "entities" / "people" / "me.md",
                {
                    "type": "demonstrates_skill",
                    "target": skill_id,
                    "properties": {
                        "source": "linkedin",
                        "skill_name": skill.name,
                    },
                    "source": source_path.name,
                },
            )
            demonstrates_skill_edges += 1

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_SKILLS_IMPORTER,
        "skills": len(skills),
        "demonstrates_skill_edges": demonstrates_skill_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Recommendations Importers ---

_RECOMMENDATION_FIELD_ALIASES = {
    "first_name": {"firstname", "first"},
    "last_name": {"lastname", "last"},
    "company": {"company", "companyname"},
    "job_title": {"jobtitle", "title", "role"},
    "text": {"text", "recommendationtext", "recommendation"},
    "creation_date": {"creationdate", "date"},
    "status": {"status"},
}


def parse_linkedin_recommendations_csv(
    path: str | Path,
) -> tuple[list[LinkedInRecommendation], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    header: list[str] = []
    required = {"firstname", "text"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or ("first" in keys and "text" in keys):
            header_index = index
            header = row
            break
    if header_index == -1:
        raise ValueError("LinkedIn recommendations CSV requires First Name and Text columns.")

    indexes = _field_index_for_aliases(header, _RECOMMENDATION_FIELD_ALIASES)
    if indexes.get("first_name") is None or indexes.get("text") is None:
        raise ValueError("LinkedIn recommendations CSV requires First Name and Text columns.")

    recommendations: list[LinkedInRecommendation] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(
                f"Row {row_number} had fewer columns than the header; missing fields were treated as blank."
            )

        first_name = _clean_linkedin_text(_cell(row, indexes, "first_name"))
        last_name = _clean_linkedin_text(_cell(row, indexes, "last_name"))
        text = _clean_linkedin_text(_cell(row, indexes, "text"))
        if not first_name or not text:
            warnings.append(f"Row {row_number} skipped because it had no name or text.")
            continue

        rec = LinkedInRecommendation(
            first_name=first_name,
            last_name=last_name,
            company=_clean_linkedin_text(_cell(row, indexes, "company")),
            job_title=_clean_linkedin_text(_cell(row, indexes, "job_title")),
            text=text,
            creation_date=_clean_linkedin_text(_cell(row, indexes, "creation_date")),
            status=_clean_linkedin_text(_cell(row, indexes, "status")),
        )
        duplicate_key = (
            normalize_name(rec.first_name),
            normalize_name(rec.last_name),
            normalize_name(rec.text[:30]),
        )
        if duplicate_key in seen:
            warnings.append(f"Row {row_number} skipped as a duplicate recommendation.")
            continue
        seen.add(duplicate_key)
        recommendations.append(rec)
    return recommendations, warnings


def _owner_recommendations_received_section(entries: list[dict[str, Any]]) -> str:
    lines = ["## LinkedIn Recommendations Received", ""]
    newline = "\n"
    for entry in sorted(entries, key=lambda item: str(item["recommendation"].creation_date), reverse=True):
        rec: LinkedInRecommendation = entry["recommendation"]
        person_ref = _wikilink(str(entry["person_slug"]), f"{rec.first_name} {rec.last_name}")
        role_company = ""
        if rec.job_title and rec.company:
            role_company = f" ({rec.job_title} at {rec.company})"
        elif rec.job_title or rec.company:
            role_company = f" ({rec.job_title or rec.company})"
        lines.append(f"- **From:** {person_ref}{role_company} on {rec.creation_date}")
        indent = "\n  > "
        lines.append(f"  > {rec.text.replace(newline, indent)}")
        lines.append("")
    return "\n".join(lines).strip()


def _owner_recommendations_given_section(entries: list[dict[str, Any]]) -> str:
    lines = ["## LinkedIn Recommendations Given", ""]
    newline = "\n"
    for entry in sorted(entries, key=lambda item: str(item["recommendation"].creation_date), reverse=True):
        rec: LinkedInRecommendation = entry["recommendation"]
        person_ref = _wikilink(str(entry["person_slug"]), f"{rec.first_name} {rec.last_name}")
        role_company = ""
        if rec.job_title and rec.company:
            role_company = f" ({rec.job_title} at {rec.company})"
        elif rec.job_title or rec.company:
            role_company = f" ({rec.job_title or rec.company})"
        lines.append(f"- **To:** {person_ref}{role_company} on {rec.creation_date}")
        indent = "\n  > "
        lines.append(f"  > {rec.text.replace(newline, indent)}")
        lines.append("")
    return "\n".join(lines).strip()


def _apply_other_person_recommendation_received(
    person_path: Path,
    *,
    rec: LinkedInRecommendation,
    owner_name: str,
) -> None:
    metadata, body = read_frontmatter(person_path)
    now = utc_now()
    indent = "\n  > "
    newline = "\n"
    content = (
        f"## LinkedIn Recommendations Received\n\n"
        f"- **From:** [[me|{owner_name}]] on {rec.creation_date}\n"
        f"  > {rec.text.replace(newline, indent)}"
    )
    body_before = body
    body = _managed_section(body, "linkedin-recommendations-received", content)
    if body.strip() != body_before.strip():
        metadata["updated_at"] = now
        write_frontmatter(person_path, metadata, body)


def _apply_other_person_recommendation_given(
    person_path: Path,
    *,
    rec: LinkedInRecommendation,
    owner_name: str,
) -> None:
    metadata, body = read_frontmatter(person_path)
    now = utc_now()
    indent = "\n  > "
    newline = "\n"
    content = (
        f"## LinkedIn Recommendations Given\n\n"
        f"- **To:** [[me|{owner_name}]] on {rec.creation_date}\n"
        f"  > {rec.text.replace(newline, indent)}"
    )
    body_before = body
    body = _managed_section(body, "linkedin-recommendations-given", content)
    if body.strip() != body_before.strip():
        metadata["updated_at"] = now
        write_frontmatter(person_path, metadata, body)


def import_linkedin_recommendations_received(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_recommendations_received")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    recommendations, warnings = parse_linkedin_recommendations_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn recommendations import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for rec in recommendations:
        full_name = f"{rec.first_name} {rec.last_name}".strip()
        person_path = _resolve_person(index, full_name, "")
        
        company_id = ""
        if rec.company and not _is_placeholder_company(rec.company):
            company_display, company_aliases = _resolve_company_display(rec.company)
            company_path = index.get(_company_key(company_display))
            company_id, company_path, was_created = _read_or_create_entity(
                root,
                entity_type="company",
                name=company_display,
                source_file=source_path.name,
                extractor=LINKEDIN_RECOMMENDATIONS_RECEIVED_IMPORTER,
                properties={},
                tags=["linkedin"],
                body_append=_company_body(company_display, owner_name),
                existing_path=company_path,
                aliases=[rec.company, *company_aliases],
            )
            index.setdefault(_company_key(company_display), company_path)
            company_ids.add(company_id)

        person_id, person_path, person_created = _read_or_create_entity(
            root,
            entity_type="person",
            name=full_name,
            source_file=source_path.name,
            extractor=LINKEDIN_RECOMMENDATIONS_RECEIVED_IMPORTER,
            properties={
                "first_name": rec.first_name,
                "last_name": rec.last_name,
                "current_company": rec.company,
                "current_role": rec.job_title,
                "source": ["linkedin"],
            },
            tags=["linkedin"],
            body_append=f"LinkedIn contact of {owner_name}.",
            existing_path=person_path,
        )
        index.setdefault(_name_key("person", full_name), person_path)
        
        target_list = created if person_created else updated
        target_list.append(
            {
                "id": person_id,
                "name": full_name,
                "file_path": str(person_path.relative_to(root)),
            }
        )

        _apply_other_person_recommendation_given(person_path, rec=rec, owner_name=owner_name)

        if company_id:
            _add_relation(
                person_path,
                {
                    "type": "works_at",
                    "target": company_id,
                    "properties": {"role": rec.job_title} if rec.job_title else {},
                    "source": source_path.name,
                },
            )

        if owner_id:
            _add_relation(
                person_path,
                {
                    "type": "knows",
                    "target": owner_id,
                    "properties": {"channel": "linkedin"},
                    "source": source_path.name,
                },
            )

        entries.append(
            {
                "recommendation": rec,
                "person_id": person_id,
                "person_slug": person_path.stem,
            }
        )

    metadata, body = read_frontmatter(owner_path)
    body_before = body
    body = _managed_section(body, "linkedin-recommendations-received", _owner_recommendations_received_section(entries))
    if body.strip() != body_before.strip():
        metadata["updated_at"] = utc_now()
        write_frontmatter(owner_path, metadata, body)

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_RECOMMENDATIONS_RECEIVED_IMPORTER,
        "recommendations": len(recommendations),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_recommendations_given(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_recommendations_given")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    recommendations, warnings = parse_linkedin_recommendations_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn recommendations import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for rec in recommendations:
        full_name = f"{rec.first_name} {rec.last_name}".strip()
        person_path = _resolve_person(index, full_name, "")
        
        company_id = ""
        if rec.company and not _is_placeholder_company(rec.company):
            company_display, company_aliases = _resolve_company_display(rec.company)
            company_path = index.get(_company_key(company_display))
            company_id, company_path, was_created = _read_or_create_entity(
                root,
                entity_type="company",
                name=company_display,
                source_file=source_path.name,
                extractor=LINKEDIN_RECOMMENDATIONS_GIVEN_IMPORTER,
                properties={},
                tags=["linkedin"],
                body_append=_company_body(company_display, owner_name),
                existing_path=company_path,
                aliases=[rec.company, *company_aliases],
            )
            index.setdefault(_company_key(company_display), company_path)
            company_ids.add(company_id)

        person_id, person_path, person_created = _read_or_create_entity(
            root,
            entity_type="person",
            name=full_name,
            source_file=source_path.name,
            extractor=LINKEDIN_RECOMMENDATIONS_GIVEN_IMPORTER,
            properties={
                "first_name": rec.first_name,
                "last_name": rec.last_name,
                "current_company": rec.company,
                "current_role": rec.job_title,
                "source": ["linkedin"],
            },
            tags=["linkedin"],
            body_append=f"LinkedIn contact of {owner_name}.",
            existing_path=person_path,
        )
        index.setdefault(_name_key("person", full_name), person_path)
        
        target_list = created if person_created else updated
        target_list.append(
            {
                "id": person_id,
                "name": full_name,
                "file_path": str(person_path.relative_to(root)),
            }
        )

        _apply_other_person_recommendation_received(person_path, rec=rec, owner_name=owner_name)

        if company_id:
            _add_relation(
                person_path,
                {
                    "type": "works_at",
                    "target": company_id,
                    "properties": {"role": rec.job_title} if rec.job_title else {},
                    "source": source_path.name,
                },
            )

        if owner_id:
            _add_relation(
                person_path,
                {
                    "type": "knows",
                    "target": owner_id,
                    "properties": {"channel": "linkedin"},
                    "source": source_path.name,
                },
            )

        entries.append(
            {
                "recommendation": rec,
                "person_id": person_id,
                "person_slug": person_path.stem,
            }
        )

    metadata, body = read_frontmatter(owner_path)
    body_before = body
    body = _managed_section(body, "linkedin-recommendations-given", _owner_recommendations_given_section(entries))
    if body.strip() != body_before.strip():
        metadata["updated_at"] = utc_now()
        write_frontmatter(owner_path, metadata, body)

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_RECOMMENDATIONS_GIVEN_IMPORTER,
        "recommendations": len(recommendations),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Saved Jobs, Job Applications, and Invitations Importers (Phase 2) ---

@dataclass(frozen=True)
class LinkedInSavedJob:
    saved_date: str
    job_url: str
    job_title: str
    company_name: str


@dataclass(frozen=True)
class LinkedInJobApplication:
    application_date: str
    contact_email: str
    contact_phone: str
    company_name: str
    job_title: str
    job_url: str
    resume_name: str
    question_and_answers: str


@dataclass(frozen=True)
class LinkedInInvitation:
    from_name: str
    to_name: str
    sent_at: str
    message: str
    direction: str
    inviter_url: str
    invitee_url: str


@dataclass(frozen=True)
class LinkedInMessage:
    conversation_id: str
    conversation_title: str
    from_name: str
    sender_profile_url: str
    to_name: str
    recipient_profile_urls: str
    date: str
    subject: str
    content: str
    folder: str
    attachments: str


@dataclass(frozen=True)
class LinkedInEndorsement:
    endorsement_date: str
    skill_name: str
    first_name: str
    last_name: str
    public_url: str
    status: str


@dataclass(frozen=True)
class LinkedInEvent:
    name: str
    time: str
    status: str
    external_url: str


def _normalize_datetime(value: str) -> str:
    """Normalize a LinkedIn timestamp/date string to ISO YYYY-MM-DD or YYYY-MM-DD HH:MM."""
    raw = value.strip()
    if not raw:
        return ""
    # E.g. "5/13/26, 1:54 AM" or "12/25/25, 10:21 AM" or "3/8/26, 4:55 AM"
    for fmt in ("%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p", "%d/%m/%y, %I:%M %p"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    # Fallback to date-only formats, e.g. "5/13/26"
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            parsed = dt.datetime.strptime(raw.split(",")[0].strip(), fmt)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return raw


def parse_linkedin_saved_jobs_csv(path: str | Path) -> tuple[list[LinkedInSavedJob], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    required = {"saveddate", "jobtitle", "companyname"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or ("joburl" in keys and "companyname" in keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn saved jobs CSV requires Saved Date, Job Title, and Company Name columns.")

    header = rows[header_index]
    aliases_by_field = {
        "saved_date": {"saveddate", "date"},
        "job_url": {"joburl", "url"},
        "job_title": {"jobtitle", "title"},
        "company_name": {"companyname", "company"},
    }
    indexes = _field_index_for_aliases(header, aliases_by_field)

    saved_jobs: list[LinkedInSavedJob] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        company_name = _clean_linkedin_text(_cell(row, indexes, "company_name"))
        job_title = _clean_linkedin_text(_cell(row, indexes, "job_title"))
        if not company_name or not job_title:
            warnings.append(f"Row {row_number} skipped because it had no company or job title.")
            continue

        job = LinkedInSavedJob(
            saved_date=_normalize_datetime(_cell(row, indexes, "saved_date")),
            job_url=_cell(row, indexes, "job_url"),
            job_title=job_title,
            company_name=company_name,
        )
        dup_key = (normalize_name(job.company_name), normalize_name(job.job_title))
        if dup_key in seen:
            warnings.append(f"Row {row_number} skipped as duplicate saved job.")
            continue
        seen.add(dup_key)
        saved_jobs.append(job)
    return saved_jobs, warnings


def parse_linkedin_job_applications_csv(path: str | Path) -> tuple[list[LinkedInJobApplication], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    required = {"applicationdate", "companyname", "jobtitle"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn job applications CSV requires Application Date, Company Name, and Job Title columns.")

    header = rows[header_index]
    aliases_by_field = {
        "application_date": {"applicationdate", "date"},
        "contact_email": {"contactemail", "email"},
        "contact_phone": {"contactphonenumber", "contactphone", "phone"},
        "company_name": {"companyname", "company"},
        "job_title": {"jobtitle", "title"},
        "job_url": {"joburl", "url"},
        "resume_name": {"resumename", "resume"},
        "question_and_answers": {"questionandanswers", "qna", "questions"},
    }
    indexes = _field_index_for_aliases(header, aliases_by_field)

    applications: list[LinkedInJobApplication] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        company_name = _clean_linkedin_text(_cell(row, indexes, "company_name"))
        job_title = _clean_linkedin_text(_cell(row, indexes, "job_title"))
        if not company_name or not job_title:
            warnings.append(f"Row {row_number} skipped because it had no company or job title.")
            continue

        app = LinkedInJobApplication(
            application_date=_normalize_datetime(_cell(row, indexes, "application_date")),
            contact_email=_cell(row, indexes, "contact_email"),
            contact_phone=_cell(row, indexes, "contact_phone"),
            company_name=company_name,
            job_title=job_title,
            job_url=_cell(row, indexes, "job_url"),
            resume_name=_cell(row, indexes, "resume_name"),
            question_and_answers=_cell(row, indexes, "question_and_answers"),
        )
        dup_key = (normalize_name(app.company_name), normalize_name(app.job_title), app.application_date)
        if dup_key in seen:
            warnings.append(f"Row {row_number} skipped as duplicate job application.")
            continue
        seen.add(dup_key)
        applications.append(app)
    return applications, warnings


def parse_linkedin_invitations_csv(path: str | Path) -> tuple[list[LinkedInInvitation], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    required = {"from", "to", "sentat", "direction"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or ("direction" in keys and "inviterprofileurl" in keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn invitations CSV requires From, To, Sent At, and Direction columns.")

    header = rows[header_index]
    aliases_by_field = {
        "from_name": {"from"},
        "to_name": {"to"},
        "sent_at": {"sentat", "date", "time"},
        "message": {"message", "note"},
        "direction": {"direction"},
        "inviter_url": {"inviterprofileurl", "inviterurl"},
        "invitee_url": {"inviteeprofileurl", "inviteeurl"},
    }
    indexes = _field_index_for_aliases(header, aliases_by_field)

    invitations: list[LinkedInInvitation] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        from_name = _clean_linkedin_text(_cell(row, indexes, "from_name"))
        to_name = _clean_linkedin_text(_cell(row, indexes, "to_name"))
        if not from_name or not to_name:
            warnings.append(f"Row {row_number} skipped because it had no names.")
            continue

        inv = LinkedInInvitation(
            from_name=from_name,
            to_name=to_name,
            sent_at=_normalize_datetime(_cell(row, indexes, "sent_at")),
            message=_clean_linkedin_text(_cell(row, indexes, "message")),
            direction=_clean_linkedin_text(_cell(row, indexes, "direction")),
            inviter_url=_cell(row, indexes, "inviter_url"),
            invitee_url=_cell(row, indexes, "invitee_url"),
        )
        dup_key = (normalize_name(inv.from_name), normalize_name(inv.to_name), inv.sent_at)
        if dup_key in seen:
            warnings.append(f"Row {row_number} skipped as duplicate invitation.")
            continue
        seen.add(dup_key)
        invitations.append(inv)
    return invitations, warnings


def _parse_qna(qna_text: str) -> dict[str, str]:
    if not qna_text:
        return {}
    qna = {}
    items = qna_text.split(" | ")
    for item in items:
        if ":" in item:
            parts = item.split(":")
            question = ":".join(parts[:-1]).strip()
            answer = parts[-1].strip()
            if question:
                qna[question] = answer
    return qna


def _opportunity_qna_markdown(qna_dict: dict[str, str]) -> str:
    if not qna_dict:
        return ""
    lines = ["## Screening Questions & Answers", ""]
    for q, a in qna_dict.items():
        lines.append(f"- **Q:** {q}")
        lines.append(f"  - **A:** {a}")
    return "\n".join(lines)


def import_linkedin_saved_jobs(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_saved_jobs")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    saved_jobs, warnings = parse_linkedin_saved_jobs_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn saved jobs import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()

    for job in saved_jobs:
        company_display, company_aliases = _resolve_company_display(job.company_name)
        company_path = index.get(_company_key(company_display))
        company_id, company_path, was_created = _read_or_create_entity(
            root,
            entity_type="company",
            name=company_display,
            source_file=source_path.name,
            extractor=LINKEDIN_SAVED_JOBS_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin"],
            body_append=_company_body(company_display, owner_name),
            existing_path=company_path,
            aliases=[job.company_name, *company_aliases],
        )
        index.setdefault(_company_key(company_display), company_path)
        index.setdefault(_company_key(job.company_name), company_path)
        company_ids.add(company_id)
        
        target_list = created if was_created else updated
        target_list.append({"id": company_id, "name": company_display, "file_path": str(company_path.relative_to(root))})

        opportunity_name = f"{job.job_title} at {company_display}"
        opportunity_path = index.get(_name_key("opportunity", opportunity_name))
        
        opp_id, opp_path, opp_created = _read_or_create_entity(
            root,
            entity_type="opportunity",
            name=opportunity_name,
            source_file=source_path.name,
            extractor=LINKEDIN_SAVED_JOBS_IMPORTER,
            properties={
                "role": job.job_title,
                "company": company_display,
                "status": "saved",
                "saved_at": job.saved_date,
                "job_url": job.job_url,
                "source": ["linkedin"],
            },
            tags=["linkedin", "saved-job"],
            body_append=f"Saved job opportunity at [[{company_path.stem}|{company_display}]].",
            existing_path=opportunity_path,
        )
        index.setdefault(_name_key("opportunity", opportunity_name), opp_path)
        
        target_list = created if opp_created else updated
        target_list.append({"id": opp_id, "name": opportunity_name, "file_path": str(opp_path.relative_to(root))})

        if owner_id:
            _add_relation(
                opp_path,
                {
                    "type": "targets",
                    "target": owner_id,
                    "direction": "incoming",
                    "properties": {
                        "status": "saved",
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                },
            )
        _add_relation(
            opp_path,
            {
                "type": "targets",
                "target": company_id,
                "properties": {
                    "source": "linkedin",
                },
                "source": source_path.name,
            },
        )

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_SAVED_JOBS_IMPORTER,
        "saved_jobs": len(saved_jobs),
        "companies": len(company_ids),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_job_applications(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_job_applications")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    applications, warnings = parse_linkedin_job_applications_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn job applications import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    company_ids: set[str] = set()

    for app in applications:
        company_display, company_aliases = _resolve_company_display(app.company_name)
        company_path = index.get(_company_key(company_display))
        company_id, company_path, was_created = _read_or_create_entity(
            root,
            entity_type="company",
            name=company_display,
            source_file=source_path.name,
            extractor=LINKEDIN_JOB_APPLICATIONS_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin"],
            body_append=_company_body(company_display, owner_name),
            existing_path=company_path,
            aliases=[app.company_name, *company_aliases],
        )
        index.setdefault(_company_key(company_display), company_path)
        index.setdefault(_company_key(app.company_name), company_path)
        company_ids.add(company_id)
        
        target_list = created if was_created else updated
        target_list.append({"id": company_id, "name": company_display, "file_path": str(company_path.relative_to(root))})

        opportunity_name = f"{app.job_title} at {company_display}"
        opportunity_path = index.get(_name_key("opportunity", opportunity_name))
        
        qna_dict = _parse_qna(app.question_and_answers)
        
        body_parts = [
            f"Job opportunity for {app.job_title} at [[{company_path.stem}|{company_display}]].",
            "",
            "## Application Details",
            f"- **Applied on:** {app.application_date}",
        ]
        if app.contact_email:
            body_parts.append(f"- **Contact Email:** {app.contact_email}")
        if app.contact_phone:
            body_parts.append(f"- **Contact Phone:** {app.contact_phone}")
        if app.resume_name:
            body_parts.append(f"- **Resume Used:** {app.resume_name}")
        if app.job_url:
            body_parts.append(f"- **Job Listing:** {app.job_url}")
            
        if qna_dict:
            body_parts.append("")
            body_parts.append(_opportunity_qna_markdown(qna_dict))
            
        opp_body = "\n".join(body_parts)

        opp_properties = {
            "role": app.job_title,
            "company": company_display,
            "status": "applied",
            "applied_on": app.application_date,
            "resume_name": app.resume_name,
            "contact_email": app.contact_email,
            "contact_phone": app.contact_phone,
            "job_url": app.job_url,
            "source": ["linkedin"],
        }
        if qna_dict:
            opp_properties["questions_and_answers"] = qna_dict

        opp_id, opp_path, opp_created = _read_or_create_entity(
            root,
            entity_type="opportunity",
            name=opportunity_name,
            source_file=source_path.name,
            extractor=LINKEDIN_JOB_APPLICATIONS_IMPORTER,
            properties=opp_properties,
            tags=["linkedin", "job-application"],
            body_append=opp_body,
            existing_path=opportunity_path,
        )
        index.setdefault(_name_key("opportunity", opportunity_name), opp_path)
        
        target_list = created if opp_created else updated
        target_list.append({"id": opp_id, "name": opportunity_name, "file_path": str(opp_path.relative_to(root))})

        if owner_id:
            _add_relation(
                opp_path,
                {
                    "type": "targets",
                    "target": owner_id,
                    "direction": "incoming",
                    "properties": {
                        "status": "applied",
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                },
            )
        _add_relation(
            opp_path,
            {
                "type": "targets",
                "target": company_id,
                "properties": {
                    "source": "linkedin",
                },
                "source": source_path.name,
            },
        )

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_JOB_APPLICATIONS_IMPORTER,
        "job_applications": len(applications),
        "companies": len(company_ids),
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_invitations(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_invitations")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    invitations, warnings = parse_linkedin_invitations_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn invitations import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    knows_edges = 0

    for inv in invitations:
        is_incoming = inv.direction.strip().upper() == "INCOMING"
        other_name = inv.from_name if is_incoming else inv.to_name
        other_url = inv.inviter_url if is_incoming else inv.invitee_url
        other_normalized_url = _normalize_url(other_url)

        person_path = _resolve_person(index, other_name, other_normalized_url)
        
        body_append = f"LinkedIn contact of {owner_name}."
        message_content = ""
        if inv.message:
            message_content = (
                f"## LinkedIn Invitation\n\n"
                f"- **Direction:** {inv.direction}\n"
                f"- **Sent At:** {inv.sent_at}\n"
                f"- **Message:**\n"
                f"  > {inv.message.replace(chr(10), chr(10) + '  > ')}\n"
            )

        person_id, person_path, person_created = _read_or_create_entity(
            root,
            entity_type="person",
            name=other_name,
            source_file=source_path.name,
            extractor=LINKEDIN_INVITATIONS_IMPORTER,
            properties={
                "linkedin_url": other_url,
                "source": ["linkedin"],
            },
            tags=["linkedin"],
            body_append=body_append,
            existing_path=person_path,
        )
        name_key = _name_key("person", other_name)
        if other_normalized_url:
            index[_url_key(other_normalized_url)] = person_path
            if index.get(name_key) == person_path:
                del index[name_key]
        else:
            index.setdefault(name_key, person_path)

        target_list = created if person_created else updated
        target_list.append({"id": person_id, "name": other_name, "file_path": str(person_path.relative_to(root))})

        if inv.message:
            metadata, body = read_frontmatter(person_path)
            body_before = body
            body = _managed_section(body, "linkedin-invitation", message_content)
            if body.strip() != body_before.strip():
                metadata["updated_at"] = utc_now()
                write_frontmatter(person_path, metadata, body)

        if owner_id:
            _add_relation(
                person_path,
                {
                    "type": "knows",
                    "target": owner_id,
                    "properties": {
                        "channel": "linkedin",
                        "invitation_sent_at": inv.sent_at,
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                },
            )
            knows_edges += 1

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_INVITATIONS_IMPORTER,
        "invitations": len(invitations),
        "knows_edges": knows_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def _is_promotional_message(content: str, from_name: str, url: str, num_messages: int) -> bool:
    body_lower = content.lower()
    if "spinmail-quill-editor" in body_lower or "%firstname%" in body_lower or "%lastname%" in body_lower:
        return True
    if num_messages == 1:
        if not url or url.strip() == "":
            if "<p" in body_lower or "<strong" in body_lower or "<br" in body_lower or "<a" in body_lower:
                return True
            promo_kws = ["online mba", "nit sikkim", "dy patil", "salesforce", "stream the must-attend event", "openclaw agent"]
            if any(kw in body_lower for kw in promo_kws):
                return True
    return False


# --- LinkedIn Messages Importer (Phase 3) ---

_MESSAGE_FIELD_ALIASES = {
    "conversation_id": {"conversationid", "id"},
    "conversation_title": {"conversationtitle", "title"},
    "from_name": {"from"},
    "sender_profile_url": {"senderprofileurl", "senderurl"},
    "to_name": {"to"},
    "recipient_profile_urls": {"recipientprofileurls", "recipienturls"},
    "date": {"date", "datetime", "timestamp"},
    "subject": {"subject"},
    "content": {"content", "text", "body"},
    "folder": {"folder"},
    "attachments": {"attachments", "attachment"},
}


def parse_linkedin_messages_csv(path: str | Path) -> tuple[list[LinkedInMessage], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    required = {"conversationid", "from", "to", "date"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or ("conversationid" in keys and "content" in keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn messages CSV requires Conversation ID, From, To, and Date columns.")

    header = rows[header_index]
    indexes = _field_index_for_aliases(header, _MESSAGE_FIELD_ALIASES)

    messages: list[LinkedInMessage] = []
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        conv_id = _cell(row, indexes, "conversation_id")
        from_name = _clean_linkedin_text(_cell(row, indexes, "from_name"))
        to_name = _clean_linkedin_text(_cell(row, indexes, "to_name"))
        if not conv_id or not from_name:
            warnings.append(f"Row {row_number} skipped because it had no conversation ID or sender name.")
            continue

        if any(c.isspace() for c in conv_id) or len(conv_id) > 100:
            warnings.append(f"Row {row_number} skipped because conversation ID is malformed (contains spaces or is too long).")
            continue
        if len(from_name) > 100 or "\n" in from_name or "http://" in from_name or "https://" in from_name:
            warnings.append(f"Row {row_number} skipped because sender name is malformed.")
            continue
        if len(to_name) > 200 or "\n" in to_name or "http://" in to_name or "https://" in to_name:
            warnings.append(f"Row {row_number} skipped because recipient name(s) is malformed.")
            continue

        msg = LinkedInMessage(
            conversation_id=conv_id,
            conversation_title=_clean_linkedin_text(_cell(row, indexes, "conversation_title")),
            from_name=from_name,
            sender_profile_url=_cell(row, indexes, "sender_profile_url"),
            to_name=to_name,
            recipient_profile_urls=_cell(row, indexes, "recipient_profile_urls"),
            date=_normalize_datetime(_cell(row, indexes, "date")),
            subject=_clean_linkedin_text(_cell(row, indexes, "subject")),
            content=_clean_linkedin_text(_cell(row, indexes, "content")),
            folder=_clean_linkedin_text(_cell(row, indexes, "folder")),
            attachments=_cell(row, indexes, "attachments"),
        )
        messages.append(msg)
    return messages, warnings


def import_linkedin_messages(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_messages")
    from synapse.identity import IdentityIndex
    from synapse.index import connect as _connect

    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    messages, warnings = parse_linkedin_messages_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn messages import requires the owner entity at entities/people/me.md.")

    # Build IdentityIndex for the shared cascade (T8.4)
    cfg = load_config(root)
    identity_cfg = cfg.get("identity") or {}
    _conn = _connect(root)
    try:
        identity_idx = IdentityIndex.build(
            _conn,
            owner_emails=identity_cfg.get("owner_emails") or [],
            owner_phones=identity_cfg.get("owner_phones") or [],
            owner_id=owner_id or "me",
        )
    finally:
        _conn.close()

    # Group messages by conversation ID
    by_conv: dict[str, list[LinkedInMessage]] = {}
    for m in messages:
        by_conv.setdefault(m.conversation_id, []).append(m)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    skipped_promo = 0
    participated_in_edges = 0

    for conv_id, msgs in by_conv.items():
        # Sort chronologically (oldest first)
        msgs_sorted = sorted(msgs, key=lambda x: x.date or "")

        # Check if conversation is promotional
        is_promo = False
        for m in msgs_sorted:
            if _is_promotional_message(m.content, m.from_name, m.sender_profile_url, len(msgs_sorted)):
                is_promo = True
                break

        if is_promo:
            skipped_promo += 1
            continue

        # Determine participants other than the owner
        owner_name_norm = normalize_name(owner_name)
        owner_url = ""
        owner_meta, _ = read_frontmatter(owner_path)
        if isinstance(owner_meta.get("properties"), dict):
            owner_url = _normalize_url(owner_meta["properties"].get("linkedin_url") or "")

        other_participants_info: list[tuple[str, str]] = []
        seen_participant_keys = set()

        for m in msgs_sorted:
            sender_norm = normalize_name(m.from_name)
            sender_url_norm = _normalize_url(m.sender_profile_url)
            is_sender_owner = (sender_norm == owner_name_norm) or (owner_url and sender_url_norm == owner_url)

            if not is_sender_owner and m.from_name:
                p_key = (normalize_name(m.from_name), sender_url_norm)
                if p_key not in seen_participant_keys:
                    seen_participant_keys.add(p_key)
                    other_participants_info.append((m.from_name, m.sender_profile_url))

            recip_urls = [r.strip() for r in m.recipient_profile_urls.split(",") if r.strip()]
            # A comma is also valid inside one LinkedIn display name (for
            # example, "Jane Example, Ph.D."). When LinkedIn supplies exactly
            # one recipient URL, the whole To field is one recipient.
            credential_suffix = re.search(
                r",\s*(?:ph\.?d\.?|dphil|m\.?d\.?|cfa|cpa|pmp|mba|ca)\s*$",
                m.to_name,
                flags=re.IGNORECASE,
            )
            if len(recip_urls) == 1 and credential_suffix:
                recip_names = [m.to_name.strip()] if m.to_name.strip() else []
            else:
                recip_names = [r.strip() for r in m.to_name.split(",") if r.strip()]

            for i in range(max(len(recip_names), len(recip_urls))):
                r_name = recip_names[i] if i < len(recip_names) else ""
                r_url = recip_urls[i] if i < len(recip_urls) else ""
                r_name_norm = normalize_name(r_name)
                r_url_norm = _normalize_url(r_url)
                is_recip_owner = (r_name_norm == owner_name_norm) or (owner_url and r_url_norm == owner_url)

                if not is_recip_owner and r_name:
                    p_key = (normalize_name(r_name), r_url_norm)
                    if p_key not in seen_participant_keys:
                        seen_participant_keys.add(p_key)
                        other_participants_info.append((r_name, r_url))

        if not other_participants_info:
            other_participants_info = [("LinkedIn Member", "")]

        other_person_ids: list[str] = []
        other_person_paths: list[Path] = []
        other_display_names = []

        for p_name, p_url in other_participants_info:
            p_url_norm = _normalize_url(p_url)

            if normalize_name(p_name) == normalize_name("LinkedIn Member"):
                person_path = index.get(_name_key("person", "LinkedIn Member"))
                p_id, person_path, person_created = _read_or_create_entity(
                    root,
                    entity_type="person",
                    name="LinkedIn Member",
                    source_file=source_path.name,
                    extractor=LINKEDIN_MESSAGES_IMPORTER,
                    properties={"source": ["linkedin"]},
                    tags=["linkedin", "placeholder"],
                    body_append=(
                        "LinkedIn Member (blocked or deleted user)."
                        if person_path is None
                        else ""
                    ),
                    existing_path=person_path,
                    preserve_existing_authority=True,
                )
                index.setdefault(_name_key("person", "LinkedIn Member"), person_path)
            else:
                # Use IdentityIndex for the shared cascade.
                # LinkedIn messages always carry a profile URL when the sender
                # is a real user, so step 1 (url) fires in practice; the
                # remaining steps future-proof the path for other sources.
                resolution = identity_idx.resolve({
                    "linkedin_url": p_url,
                    "name": p_name,
                })

                # Derive the existing_path from the old per-file index if
                # the cascade didn't find anything via IdentityIndex (first
                # run when the person doesn't exist yet) so the
                # _read_or_create_entity call below works as before.
                if resolution.id is not None:
                    # Resolve path from the flat file index for known entities.
                    person_path = (
                        index.get(_url_key(p_url_norm))
                        or index.get(_name_key("person", p_name))
                    )
                    unresolved_tag = False
                else:
                    person_path = _resolve_person(index, p_name, p_url_norm)
                    # Only tag as identity-unresolved when name-only ambiguity:
                    # LinkedIn always supplies a profile URL for real contacts,
                    # so this path means the URL is absent and name is ambiguous.
                    unresolved_tag = (
                        person_path is None
                        and not p_url_norm
                        and bool(resolution.candidates)
                    )

                extra_tags = ["linkedin"]
                if unresolved_tag:
                    extra_tags.append("identity-unresolved")

                p_id, person_path, person_created = _read_or_create_entity(
                    root,
                    entity_type="person",
                    name=p_name,
                    source_file=source_path.name,
                    extractor=LINKEDIN_MESSAGES_IMPORTER,
                    properties={
                        "linkedin_url": p_url,
                        "source": ["linkedin"],
                    },
                    tags=extra_tags,
                    body_append=(
                        f"LinkedIn contact of {owner_name}."
                        if person_path is None
                        else ""
                    ),
                    existing_path=person_path,
                    preserve_existing_authority=True,
                )
                name_key = _name_key("person", p_name)
                if p_url_norm:
                    index[_url_key(p_url_norm)] = person_path
                    if index.get(name_key) == person_path:
                        del index[name_key]
                else:
                    index.setdefault(name_key, person_path)

            other_person_ids.append(p_id)
            other_person_paths.append(person_path)
            other_display_names.append(p_name)

            target_list = created if person_created else updated
            target_list.append({
                "id": p_id,
                "name": p_name,
                "file_path": str(person_path.relative_to(root))
            })

        conv_title = ", ".join(other_display_names)
        conv_name = f"Conversation with {conv_title}"
        
        existing_conv_path = index.get(("conversation", f"id:{conv_id}"))

        body_lines = ["## Chat Log", ""]
        for m in msgs_sorted:
            body_lines.append(f"**{m.date} ({m.from_name}):**")
            body_lines.append(m.content)
            if m.attachments:
                body_lines.append(f"- **Attachment:** {m.attachments}")
            body_lines.append("")

        conv_body = "\n".join(body_lines)

        properties = {
            "conversation_id": conv_id,
            "message_count": len(msgs_sorted),
            "started_at": msgs_sorted[0].date,
            "last_message_at": msgs_sorted[-1].date,
            "participants": [owner_name, *other_display_names],
            "source": ["linkedin"],
        }

        conv_uuid, conv_path, conv_created = _read_or_create_entity(
            root,
            entity_type="conversation",
            name=conv_name,
            source_file=source_path.name,
            extractor=LINKEDIN_MESSAGES_IMPORTER,
            properties=properties,
            tags=["linkedin", "conversation"],
            body_append=(
                f"LinkedIn conversation with {conv_title}."
                if existing_conv_path is None
                else ""
            ),
            existing_path=existing_conv_path,
        )
        
        index[("conversation", f"id:{conv_id}")] = conv_path

        metadata, body = read_frontmatter(conv_path)
        body_before = body
        body = _managed_section(body, "linkedin-chat-log", conv_body)
        if body.strip() != body_before.strip():
            write_frontmatter(conv_path, metadata, body)

        target_list = created if conv_created else updated
        target_list.append({
            "id": conv_uuid,
            "name": conv_name,
            "file_path": str(conv_path.relative_to(root))
        })

        participants_to_add = []
        if owner_id:
            participants_to_add.append(owner_id)
        participants_to_add.extend(other_person_ids)

        # This importer owns the conversation title and participant fields.
        # Reconcile them exactly on re-import so parser fixes remove previously
        # generated phantom participants instead of only adding better data.
        metadata, body = read_frontmatter(conv_path)
        before_metadata = copy.deepcopy(metadata)
        before_body = body
        old_name = str(metadata.get("name") or "")
        metadata["name"] = conv_name
        conv_properties = metadata.setdefault("properties", {})
        if not isinstance(conv_properties, dict):
            conv_properties = {}
            metadata["properties"] = conv_properties
        conv_properties["participants"] = properties["participants"]
        valid_participant_ids = set(participants_to_add)
        metadata["relations"] = [
            relation
            for relation in metadata.get("relations") or []
            if not (
                isinstance(relation, dict)
                and relation.get("type") == "participated_in"
                and relation.get("source") == source_path.name
                and relation.get("target") not in valid_participant_ids
            )
        ]
        if old_name and old_name != conv_name:
            lines = body.splitlines()
            if lines and lines[0] == f"# {old_name}":
                lines[0] = f"# {conv_name}"
                body = "\n".join(lines)
                if before_body.endswith("\n"):
                    body += "\n"
        if metadata != before_metadata or body != before_body:
            metadata["review_status"] = "proposed"
            metadata["updated_at"] = utc_now()
            write_frontmatter(conv_path, metadata, body)

        for part_id in participants_to_add:
            _add_relation(
                conv_path,
                {
                    "type": "participated_in",
                    "target": part_id,
                    "direction": "incoming",
                    "properties": {
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                }
            )
            participated_in_edges += 1

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_MESSAGES_IMPORTER,
        "conversations": len(by_conv) - skipped_promo,
        "skipped_promotions": skipped_promo,
        "participated_in_edges": participated_in_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Endorsements Importers (Phase 3) ---

_ENDORSEMENT_FIELD_ALIASES = {
    "endorsement_date": {"endorsementdate", "date"},
    "skill_name": {"skillname", "skill"},
    "first_name": {"endorseefirstname", "endorserfirstname", "firstname", "first"},
    "last_name": {"endorseelastname", "endorserlastname", "lastname", "last"},
    "public_url": {"endorseepublicurl", "endorserpublicurl", "publicurl", "url"},
    "status": {"endorsementstatus", "status"},
}


def parse_linkedin_endorsements_csv(path: str | Path) -> tuple[list[LinkedInEndorsement], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if "skillname" in keys and ("endorsementstatus" in keys or "status" in keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn endorsements CSV requires Skill Name, Endorsement Date, and Status columns.")

    header = rows[header_index]
    indexes = _field_index_for_aliases(header, _ENDORSEMENT_FIELD_ALIASES)

    endorsements: list[LinkedInEndorsement] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        skill_name = _clean_linkedin_text(_cell(row, indexes, "skill_name"))
        first_name = _clean_linkedin_text(_cell(row, indexes, "first_name"))
        last_name = _clean_linkedin_text(_cell(row, indexes, "last_name"))
        if not skill_name or not (first_name or last_name):
            warnings.append(f"Row {row_number} skipped because it had no skill name or endorser/endorsee name.")
            continue

        raw_date = _cell(row, indexes, "endorsement_date")
        normalized_date = _normalize_datetime(raw_date)

        end = LinkedInEndorsement(
            endorsement_date=normalized_date,
            skill_name=skill_name,
            first_name=first_name,
            last_name=last_name,
            public_url=_cell(row, indexes, "public_url"),
            status=_clean_linkedin_text(_cell(row, indexes, "status")),
        )
        dup_key = (normalize_name(end.skill_name), normalize_name(end.first_name + " " + end.last_name), end.endorsement_date)
        if dup_key in seen:
            warnings.append(f"Row {row_number} skipped as duplicate endorsement.")
            continue
        seen.add(dup_key)
        endorsements.append(end)
    return endorsements, warnings


def import_linkedin_endorsements_received(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_endorsements_received")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    endorsements, warnings = parse_linkedin_endorsements_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn endorsements import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    endorsed_skill_edges = 0

    received_by_skill: dict[str, list[tuple[str, str, str]]] = {}

    for end in endorsements:
        endorser_name = f"{end.first_name} {end.last_name}".strip()
        if not endorser_name:
            endorser_name = "LinkedIn Member"
        endorser_url_norm = _normalize_url(end.public_url)

        if normalize_name(endorser_name) == normalize_name("LinkedIn Member"):
            person_path = index.get(_name_key("person", "LinkedIn Member"))
            endorser_id, person_path, person_created = _read_or_create_entity(
                root,
                entity_type="person",
                name="LinkedIn Member",
                source_file=source_path.name,
                extractor=LINKEDIN_ENDORSEMENTS_RECEIVED_IMPORTER,
                properties={"source": ["linkedin"]},
                tags=["linkedin", "placeholder"],
                body_append="LinkedIn Member (blocked or deleted user).",
                existing_path=person_path,
            )
            index.setdefault(_name_key("person", "LinkedIn Member"), person_path)
        else:
            person_path = _resolve_person(index, endorser_name, endorser_url_norm)
            endorser_id, person_path, person_created = _read_or_create_entity(
                root,
                entity_type="person",
                name=endorser_name,
                source_file=source_path.name,
                extractor=LINKEDIN_ENDORSEMENTS_RECEIVED_IMPORTER,
                properties={
                    "linkedin_url": end.public_url,
                    "source": ["linkedin"],
                },
                tags=["linkedin"],
                body_append=f"LinkedIn contact of {owner_name}.",
                existing_path=person_path,
            )
            name_key = _name_key("person", endorser_name)
            if endorser_url_norm:
                index[_url_key(endorser_url_norm)] = person_path
                if index.get(name_key) == person_path:
                    del index[name_key]
            else:
                index.setdefault(name_key, person_path)

        target_list = created if person_created else updated
        target_list.append({
            "id": endorser_id,
            "name": endorser_name,
            "file_path": str(person_path.relative_to(root))
        })

        skill_path = index.get(_name_key("skill", end.skill_name))
        skill_id, skill_path, skill_created = _read_or_create_entity(
            root,
            entity_type="skill",
            name=end.skill_name,
            source_file=source_path.name,
            extractor=LINKEDIN_ENDORSEMENTS_RECEIVED_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin", "skill"],
            body_append=f"LinkedIn skill associated with {owner_name}.",
            existing_path=skill_path,
        )
        index.setdefault(_name_key("skill", end.skill_name), skill_path)
        if skill_created:
            created.append({
                "id": skill_id,
                "name": end.skill_name,
                "file_path": str(skill_path.relative_to(root))
            })

        if owner_id:
            _add_relation(
                person_path,
                {
                    "type": "endorsed_skill",
                    "target": owner_id,
                    "properties": {
                        "skill_name": end.skill_name,
                        "skill_id": skill_id,
                        "date": end.endorsement_date,
                        "status": end.status,
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                }
            )
            endorsed_skill_edges += 1

        endorser_meta, endorser_body = read_frontmatter(person_path)
        given_line = f"- Endorsed [[me|{owner_name}]] for **{end.skill_name}** on {end.endorsement_date}"
        given_lines = []
        if "## LinkedIn Endorsements Given" in endorser_body:
            parts = endorser_body.split("## LinkedIn Endorsements Given")
            subparts = parts[1].split("\n## ")
            existing_given = subparts[0].strip()
            given_lines = [line.strip() for line in existing_given.split("\n") if line.strip() and not line.strip().startswith("<!--")]
        if given_line not in given_lines:
            given_lines.append(given_line)
        given_section = "## LinkedIn Endorsements Given\n\n" + "\n".join(given_lines)
        endorser_body_before = endorser_body
        endorser_body = _managed_section(endorser_body, "linkedin-endorsements-given", given_section)
        if endorser_body.strip() != endorser_body_before.strip():
            write_frontmatter(person_path, endorser_meta, endorser_body)

        received_by_skill.setdefault(end.skill_name, []).append((
            endorser_name,
            person_path.stem,
            end.endorsement_date
        ))

    owner_meta, owner_body = read_frontmatter(owner_path)
    existing_received_lines = []
    if "## LinkedIn Endorsements Received" in owner_body:
        parts = owner_body.split("## LinkedIn Endorsements Received")
        subparts = parts[1].split("\n## ")
        existing_received_lines = [
            line.strip()
            for line in subparts[0].strip().split("\n")
            if line.strip() and not line.strip().startswith("<!--")
        ]

    received_lines_set = set(existing_received_lines)
    for skill_name, endorsers in received_by_skill.items():
        for e_name, e_slug, e_date in endorsers:
            line = f"- **{skill_name}** by [[{e_slug}|{e_name}]] on {e_date}"
            received_lines_set.add(line)

    received_section = "## LinkedIn Endorsements Received\n\n" + "\n".join(sorted(list(received_lines_set)))
    owner_body_before = owner_body
    owner_body = _managed_section(owner_body, "linkedin-endorsements-received", received_section)
    if owner_body.strip() != owner_body_before.strip():
        write_frontmatter(owner_path, owner_meta, owner_body)

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_ENDORSEMENTS_RECEIVED_IMPORTER,
        "endorsements": len(endorsements),
        "endorsed_skill_edges": endorsed_skill_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


def import_linkedin_endorsements_given(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_endorsements_given")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    endorsements, warnings = parse_linkedin_endorsements_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn endorsements import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    endorsed_skill_edges = 0

    given_by_person: dict[str, list[tuple[str, str, str, str]]] = {}

    for end in endorsements:
        endorsee_name = f"{end.first_name} {end.last_name}".strip()
        if not endorsee_name:
            endorsee_name = "LinkedIn Member"
        endorsee_url_norm = _normalize_url(end.public_url)

        if normalize_name(endorsee_name) == normalize_name("LinkedIn Member"):
            person_path = index.get(_name_key("person", "LinkedIn Member"))
            endorsee_id, person_path, person_created = _read_or_create_entity(
                root,
                entity_type="person",
                name="LinkedIn Member",
                source_file=source_path.name,
                extractor=LINKEDIN_ENDORSEMENTS_GIVEN_IMPORTER,
                properties={"source": ["linkedin"]},
                tags=["linkedin", "placeholder"],
                body_append="LinkedIn Member (blocked or deleted user).",
                existing_path=person_path,
            )
            index.setdefault(_name_key("person", "LinkedIn Member"), person_path)
        else:
            person_path = _resolve_person(index, endorsee_name, endorsee_url_norm)
            endorsee_id, person_path, person_created = _read_or_create_entity(
                root,
                entity_type="person",
                name=endorsee_name,
                source_file=source_path.name,
                extractor=LINKEDIN_ENDORSEMENTS_GIVEN_IMPORTER,
                properties={
                    "linkedin_url": end.public_url,
                    "source": ["linkedin"],
                },
                tags=["linkedin"],
                body_append=f"LinkedIn contact of {owner_name}.",
                existing_path=person_path,
            )
            name_key = _name_key("person", endorsee_name)
            if endorsee_url_norm:
                index[_url_key(endorsee_url_norm)] = person_path
                if index.get(name_key) == person_path:
                    del index[name_key]
            else:
                index.setdefault(name_key, person_path)

        target_list = created if person_created else updated
        target_list.append({
            "id": endorsee_id,
            "name": endorsee_name,
            "file_path": str(person_path.relative_to(root))
        })

        skill_path = index.get(_name_key("skill", end.skill_name))
        skill_id, skill_path, skill_created = _read_or_create_entity(
            root,
            entity_type="skill",
            name=end.skill_name,
            source_file=source_path.name,
            extractor=LINKEDIN_ENDORSEMENTS_GIVEN_IMPORTER,
            properties={"source": ["linkedin"]},
            tags=["linkedin", "skill"],
            body_append=f"LinkedIn skill associated with {owner_name}.",
            existing_path=skill_path,
        )
        index.setdefault(_name_key("skill", end.skill_name), skill_path)
        if skill_created:
            created.append({
                "id": skill_id,
                "name": end.skill_name,
                "file_path": str(skill_path.relative_to(root))
            })

        if owner_id:
            _add_relation(
                owner_path,
                {
                    "type": "endorsed_skill",
                    "target": endorsee_id,
                    "properties": {
                        "skill_name": end.skill_name,
                        "skill_id": skill_id,
                        "date": end.endorsement_date,
                        "status": end.status,
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                }
            )
            endorsed_skill_edges += 1

        endorsee_meta, endorsee_body = read_frontmatter(person_path)
        received_line = f"- **{end.skill_name}** by [[me|{owner_name}]] on {end.endorsement_date}"
        existing_received = []
        if "## LinkedIn Endorsements Received" in endorsee_body:
            parts = endorsee_body.split("## LinkedIn Endorsements Received")
            subparts = parts[1].split("\n## ")
            existing_received = [line.strip() for line in subparts[0].strip().split("\n") if line.strip() and not line.strip().startswith("<!--")]
        if received_line not in existing_received:
            existing_received.append(received_line)
        received_section = "## LinkedIn Endorsements Received\n\n" + "\n".join(existing_received)
        endorsee_body_before = endorsee_body
        endorsee_body = _managed_section(endorsee_body, "linkedin-endorsements-received", received_section)
        if endorsee_body.strip() != endorsee_body_before.strip():
            write_frontmatter(person_path, endorsee_meta, endorsee_body)

        given_by_person.setdefault(person_path.stem, []).append((
            end.skill_name,
            skill_path.stem,
            end.endorsement_date,
            endorsee_name
        ))

    owner_meta, owner_body = read_frontmatter(owner_path)
    existing_given_lines = []
    if "## LinkedIn Endorsements Given" in owner_body:
        parts = owner_body.split("## LinkedIn Endorsements Given")
        subparts = parts[1].split("\n## ")
        existing_given_lines = [
            line.strip()
            for line in subparts[0].strip().split("\n")
            if line.strip() and not line.strip().startswith("<!--")
        ]

    given_lines_set = set(existing_given_lines)
    for p_slug, skills in given_by_person.items():
        for s_name, _s_slug, s_date, p_name in skills:
            line = f"- Endorsed [[{p_slug}|{p_name}]] for **{s_name}** on {s_date}"
            given_lines_set.add(line)

    given_section = "## LinkedIn Endorsements Given\n\n" + "\n".join(sorted(list(given_lines_set)))
    owner_body_before = owner_body
    owner_body = _managed_section(owner_body, "linkedin-endorsements-given", given_section)
    if owner_body.strip() != owner_body_before.strip():
        write_frontmatter(owner_path, owner_meta, owner_body)

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_ENDORSEMENTS_GIVEN_IMPORTER,
        "endorsements": len(endorsements),
        "endorsed_skill_edges": endorsed_skill_edges,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }


# --- LinkedIn Events Importer (Phase 3) ---

_EVENT_FIELD_ALIASES = {
    "name": {"eventname", "name", "event"},
    "time": {"eventtime", "time", "date"},
    "status": {"status"},
    "external_url": {"externalurl", "url"},
}


def parse_linkedin_events_csv(path: str | Path) -> tuple[list[LinkedInEvent], list[str]]:
    source = Path(path)
    rows, warnings = _rows_from_csv(_read_csv_text(source))
    if not rows:
        return [], ["CSV file did not contain rows."]

    header_index = -1
    required = {"eventname", "status"}
    for index, row in enumerate(rows[:25]):
        keys = {_header_key(cell) for cell in row}
        if required.issubset(keys) or ("eventname" in keys and "eventtime" in keys):
            header_index = index
            break
    if header_index == -1:
        raise ValueError("LinkedIn events CSV requires Event Name and Status columns.")

    header = rows[header_index]
    indexes = _field_index_for_aliases(header, _EVENT_FIELD_ALIASES)

    events: list[LinkedInEvent] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if len(row) < len(header):
            warnings.append(f"Row {row_number} had fewer columns than the header; missing fields were treated as blank.")

        name = _clean_linkedin_text(_cell(row, indexes, "name"))
        if not name:
            warnings.append(f"Row {row_number} skipped because it had no event name.")
            continue

        ev = LinkedInEvent(
            name=name,
            time=_clean_linkedin_text(_cell(row, indexes, "time")),
            status=_clean_linkedin_text(_cell(row, indexes, "status")),
            external_url=_cell(row, indexes, "external_url"),
        )
        dup_key = (normalize_name(ev.name), normalize_name(ev.time))
        if dup_key in seen:
            warnings.append(f"Row {row_number} skipped as duplicate event.")
            continue
        seen.add(dup_key)
        events.append(ev)
    return events, warnings


def import_linkedin_events(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "import_linkedin_events")
    root = resolve_vault(vault_path if vault_path is not None else vault)
    source_path = Path(source).resolve()
    reindex(root)
    events, warnings = parse_linkedin_events_csv(source_path)
    index = _entity_index(root)
    owner_id, owner_name = _owner(root)
    owner_path = root / "entities" / "people" / "me.md"
    if not owner_path.exists():
        raise ValueError("LinkedIn events import requires the owner entity at entities/people/me.md.")

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    attended_relations = 0

    events_attended_info = []

    for ev in events:
        event_path = index.get(_name_key("event", ev.name))
        
        body_append = f"Event on LinkedIn: {ev.name}."
        if ev.time:
            body_append += f"\n- **Time:** {ev.time}"
        if ev.status:
            body_append += f"\n- **Status:** {ev.status}"
        if ev.external_url:
            body_append += f"\n- **URL:** {ev.external_url}"

        properties = {
            "time": ev.time,
            "status": ev.status,
            "external_url": ev.external_url,
            "source": ["linkedin"],
        }

        event_id, event_path, event_created = _read_or_create_entity(
            root,
            entity_type="event",
            name=ev.name,
            source_file=source_path.name,
            extractor=LINKEDIN_EVENTS_IMPORTER,
            properties=properties,
            tags=["linkedin", "event"],
            body_append=f"LinkedIn event: {ev.name}.",
            existing_path=event_path,
        )
        index.setdefault(_name_key("event", ev.name), event_path)

        metadata, body = read_frontmatter(event_path)
        body_before = body
        body = _managed_section(body, "linkedin-event-details", body_append)
        if body.strip() != body_before.strip():
            write_frontmatter(event_path, metadata, body)

        target_list = created if event_created else updated
        target_list.append({
            "id": event_id,
            "name": ev.name,
            "file_path": str(event_path.relative_to(root))
        })

        if owner_id:
            _add_relation(
                owner_path,
                {
                    "type": "attended",
                    "target": event_id,
                    "properties": {
                        "status": ev.status,
                        "time": ev.time,
                        "url": ev.external_url,
                        "source": "linkedin",
                    },
                    "source": source_path.name,
                }
            )
            attended_relations += 1

        events_attended_info.append((ev.name, event_path.stem, ev.time, ev.status))

    owner_meta, owner_body = read_frontmatter(owner_path)
    existing_events_lines = []
    if "## LinkedIn Events" in owner_body:
        parts = owner_body.split("## LinkedIn Events")
        subparts = parts[1].split("\n## ")
        existing_events_lines = [
            line.strip()
            for line in subparts[0].strip().split("\n")
            if line.strip() and not line.strip().startswith("<!--")
        ]

    events_lines_set = set(existing_events_lines)
    for name, slug, time, status in events_attended_info:
        time_part = f" ({time})" if time else ""
        status_part = f" - {status}" if status else ""
        line = f"- [[{slug}|{name}]]{time_part}{status_part}"
        events_lines_set.add(line)

    events_section = "## LinkedIn Events\n\n" + "\n".join(sorted(list(events_lines_set)))
    owner_body_before = owner_body
    owner_body = _managed_section(owner_body, "linkedin-events", events_section)
    if owner_body.strip() != owner_body_before.strip():
        write_frontmatter(owner_path, owner_meta, owner_body)

    reindex(root, full=True)
    _agent_guide_reminder()
    return {
        "source": str(source_path),
        "importer": LINKEDIN_EVENTS_IMPORTER,
        "events": len(events),
        "attended_relations": attended_relations,
        "created": created,
        "updated": updated,
        "warnings": warnings,
    }
