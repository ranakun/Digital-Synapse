"""Cross-source identity resolution helper for Digital Synapse.

Provides a deterministic match cascade used by every importer that touches
people entities:
  1. linkedin_url  (authoritative)
  2. email         (authoritative)
  3. phone         (authoritative)
  4. name + company (auto-match only if exactly one candidate)
  5. name only     → never auto-match; emit a proposal with candidates

Owner identities are resolved to ``me`` before the cascade runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from synapse.util import generate_ulid, norm_url, normalize_name, utc_now


def norm_email(value: str) -> str:
    """Casefold and strip an email address."""
    return value.strip().casefold()


def norm_phone(value: str) -> str:
    """Normalize a phone number to digits only, preserving leading country code.

    Strategy: strip all non-digit characters except a leading ``+``, then keep
    the ``+`` prefix intact so that ``+919876543210`` and ``919876543210`` are
    treated as the same number after stripping the ``+``.

    Actually we strip the ``+`` too and only keep digit characters, since
    E.164 representation (+CC NNNN…) still compares equal once we have
    digits-only from both sides.
    """
    digits = re.sub(r"[^\d]", "", value)
    return digits


# ---------------------------------------------------------------------------
# Alias-worthiness filter (design doc §Tricky bits)
# ---------------------------------------------------------------------------

_NOISE_WORDS = re.compile(r"\bvia\b", re.IGNORECASE)
_ALL_CAPS_RE = re.compile(r"^[A-Z\s\d\-_\.]+$")


def alias_worthy(display_name: str) -> bool:
    """Return True if a display name is worth recording as an alias.

    Filters out noise patterns from email display names:
    - Contains "via" (e.g. "Alex via LinkedIn")
    - Single-token strings (initials, handles — often garbage)
    - All-caps strings (typically system accounts or placeholders)
    """
    name = display_name.strip()
    if not name:
        return False
    if _NOISE_WORDS.search(name):
        return False
    tokens = name.split()
    if len(tokens) < 2:
        return False
    if _ALL_CAPS_RE.match(name):
        return False
    return True


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    """Result of a single identity-resolution attempt."""

    id: str | None
    """The resolved entity ID, or ``None`` if unresolved."""

    method: str
    """Which cascade step produced the match (or 'unresolved')."""

    candidates: list[str] = field(default_factory=list)
    """Candidate IDs when ``id`` is ``None`` (name-only ambiguity)."""


# ---------------------------------------------------------------------------
# IdentityIndex
# ---------------------------------------------------------------------------

@dataclass
class IdentityIndex:
    """In-memory identity maps built from the SQLite index.

    Built once per importer run via ``IdentityIndex.build(conn)``.
    """

    # url (norm_url) → entity_id
    _by_url: dict[str, str] = field(default_factory=dict)
    # email (norm_email) → entity_id
    _by_email: dict[str, str] = field(default_factory=dict)
    # phone (norm_phone) → entity_id
    _by_phone: dict[str, str] = field(default_factory=dict)
    # (norm_name, norm_company) → list[entity_id]; step 4 auto-matches only
    # when exactly one candidate exists (design doc 05)
    _by_name_company: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # norm_name → list[entity_id]   (includes aliases)
    _by_name: dict[str, list[str]] = field(default_factory=dict)

    # Owner-identity sets (loaded from config)
    _owner_emails: set[str] = field(default_factory=set)
    _owner_phones: set[str] = field(default_factory=set)
    _owner_id: str = "me"

    @classmethod
    def build(
        cls,
        conn,
        *,
        owner_emails: list[str] | None = None,
        owner_phones: list[str] | None = None,
        owner_id: str = "me",
    ) -> IdentityIndex:
        """One pass over the SQLite ``entities`` table to build all maps.

        ``merged_into`` redirections are followed so re-imports of old data
        attach to the survivor automatically.
        """
        idx = cls()
        idx._owner_id = owner_id
        idx._owner_emails = {norm_email(e) for e in (owner_emails or []) if e}
        idx._owner_phones = {norm_phone(p) for p in (owner_phones or []) if p}

        rows = conn.execute(
            "SELECT id, name, type, frontmatter FROM entities"
        ).fetchall()

        # Build a merged_into map: loser_id → survivor_id
        merged_into: dict[str, str] = {}
        for row in rows:
            fm = json.loads(row["frontmatter"] or "{}")
            mi = fm.get("merged_into")
            if mi:
                merged_into[str(row["id"])] = str(mi)

        def resolve_survivor(eid: str) -> str:
            """Follow merged_into chain to find the surviving entity."""
            seen = set()
            current = eid
            while current in merged_into and current not in seen:
                seen.add(current)
                current = merged_into[current]
            return current

        for row in rows:
            entity_id = str(row["id"])
            entity_type = str(row["type"] or "")
            if entity_type != "person":
                continue

            survivor_id = resolve_survivor(entity_id)
            fm = json.loads(row["frontmatter"] or "{}")
            props = fm.get("properties") or {}
            if not isinstance(props, dict):
                props = {}

            # URL index
            url_raw = str(props.get("linkedin_url") or "")
            if url_raw:
                url_norm = norm_url(url_raw)
                if url_norm and url_norm not in idx._by_url:
                    idx._by_url[url_norm] = survivor_id

            # Email index
            emails = props.get("emails") or []
            if isinstance(emails, str):
                emails = [emails]
            for email in emails:
                e_norm = norm_email(str(email))
                if e_norm and e_norm not in idx._by_email:
                    idx._by_email[e_norm] = survivor_id

            # Phone index
            phones = props.get("phones") or []
            if isinstance(phones, str):
                phones = [phones]
            for phone in phones:
                p_norm = norm_phone(str(phone))
                if p_norm and p_norm not in idx._by_phone:
                    idx._by_phone[p_norm] = survivor_id

            # Name + company index (current company only, as per design doc)
            name = str(fm.get("name") or "")
            company = str(props.get("current_company") or "")
            name_norm = normalize_name(name)
            company_norm = normalize_name(company)
            if name_norm and company_norm:
                key = (name_norm, company_norm)
                idx._by_name_company.setdefault(key, [])
                if survivor_id not in idx._by_name_company[key]:
                    idx._by_name_company[key].append(survivor_id)

            # Name / alias index (list of candidates)
            labels = [name, *(str(a) for a in (fm.get("aliases") or []))]
            for label in labels:
                label_norm = normalize_name(label)
                if label_norm:
                    idx._by_name.setdefault(label_norm, [])
                    if survivor_id not in idx._by_name[label_norm]:
                        idx._by_name[label_norm].append(survivor_id)

        return idx

    # ------------------------------------------------------------------
    # Public resolve entry-point
    # ------------------------------------------------------------------

    def resolve(self, person_facts: dict[str, Any]) -> Resolution:
        """Apply the five-step cascade and return a Resolution.

        ``person_facts`` is a plain dict with any subset of:
          - ``linkedin_url``  (str)
          - ``emails``        (list[str] or str)
          - ``phones``        (list[str] or str)
          - ``name``          (str)
          - ``current_company`` (str)

        Cascade (first hit wins):
          1. linkedin_url  → authoritative
          2. email         → authoritative
          3. phone         → authoritative
          4. name + current_company → auto-match only if exactly one candidate
          5. name only     → never auto-match; return candidates list

        Owner identities (owner_emails / owner_phones set at build time)
        resolve to ``me`` before the cascade runs.
        """

        # --- Owner short-circuit ---
        emails_raw = person_facts.get("emails") or []
        if isinstance(emails_raw, str):
            emails_raw = [emails_raw]
        for email in emails_raw:
            if norm_email(str(email)) in self._owner_emails:
                return Resolution(id=self._owner_id, method="owner:email")

        phones_raw = person_facts.get("phones") or []
        if isinstance(phones_raw, str):
            phones_raw = [phones_raw]
        for phone in phones_raw:
            if norm_phone(str(phone)) in self._owner_phones:
                return Resolution(id=self._owner_id, method="owner:phone")

        # --- Step 1: linkedin_url ---
        url_raw = str(person_facts.get("linkedin_url") or "")
        if url_raw:
            url_norm = norm_url(url_raw)
            if url_norm and url_norm in self._by_url:
                return Resolution(id=self._by_url[url_norm], method="url")

        # --- Step 2: email ---
        for email in emails_raw:
            e_norm = norm_email(str(email))
            if e_norm and e_norm in self._by_email:
                return Resolution(id=self._by_email[e_norm], method="email")

        # --- Step 3: phone ---
        for phone in phones_raw:
            p_norm = norm_phone(str(phone))
            if p_norm and p_norm in self._by_phone:
                return Resolution(id=self._by_phone[p_norm], method="phone")

        # --- Step 4: name + company (unique only) ---
        name = str(person_facts.get("name") or "")
        company = str(person_facts.get("current_company") or "")
        name_norm = normalize_name(name)
        company_norm = normalize_name(company)
        if name_norm and company_norm:
            key = (name_norm, company_norm)
            hits = self._by_name_company.get(key, [])
            if len(hits) == 1:
                return Resolution(id=hits[0], method="name+company")
            if len(hits) > 1:
                # Two people sharing name+company: never auto-match; fall
                # through to step 5 so they surface as candidates.
                candidates = list(dict.fromkeys([*hits, *self._by_name.get(name_norm, [])]))
                return Resolution(id=None, method="unresolved", candidates=candidates)

        # --- Step 5: name only → never auto-match ---
        if name_norm:
            candidates = list(self._by_name.get(name_norm, []))
            return Resolution(id=None, method="unresolved", candidates=candidates)

        return Resolution(id=None, method="unresolved", candidates=[])


# ---------------------------------------------------------------------------
# T8.3 — emit_candidates_proposal
# ---------------------------------------------------------------------------

@dataclass
class _UnresolvedItem:
    name: str
    candidates: list[str]
    evidence_note: str = ""


def emit_candidates_proposal(
    vault: str | Path,
    unresolved: list[dict[str, Any]],
) -> Path:
    """Write a Phase-5 proposal YAML to ``<vault>/proposals/pending/``.

    Each item in ``unresolved`` is a dict with:
      - ``name``       (str) – the display name that could not be resolved
      - ``candidates`` (list[str]) – entity IDs that are plausible matches
      - ``evidence``   (str, optional) – short note about the source

    Emitted ops are ``add_alias`` ops (one per candidate) with
    ``confidence: low``, giving the maintainer an actionable review target.
    ``base`` carries the candidates' content hashes at generation time so the
    proposal passes ``apply`` validation and gets stale-checked like any other.

    Returns the path of the written proposal file.
    """
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "emit_candidates_proposal")
    root = Path(vault).resolve()
    pending_dir = root / "proposals" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    proposal_id = generate_ulid()
    now = utc_now()

    ops: list[dict[str, Any]] = []
    evidence_items: list[dict[str, str]] = []
    base_ids: list[str] = []

    for item in unresolved:
        name = str(item.get("name") or "")
        candidates = [str(c) for c in (item.get("candidates") or [])]
        note = str(item.get("evidence") or f"Unresolved participant '{name}' from import")
        evidence_items.append({"file": "identity.py", "note": note})

        if candidates:
            # Suggest merge: keep first candidate, flag second+ for review
            for candidate_id in candidates:
                ops.append({
                    "op": "add_alias",
                    "id": candidate_id,
                    "alias": name,
                    "reason": (
                        f"Candidate match for unresolved participant '{name}'. "
                        f"Review before accepting."
                    ),
                })
                if candidate_id not in base_ids:
                    base_ids.append(candidate_id)
        # If no candidates, nothing to propose; the entity was already created
        # with the identity-unresolved tag.

    # Snapshot candidate hashes so apply's validation + stale check work.
    base_items: list[dict[str, str]] = []
    if base_ids:
        from synapse.index import connect

        conn = connect(root)
        try:
            for entity_id in base_ids:
                row = conn.execute(
                    "SELECT content_hash FROM entities WHERE id = ?", (entity_id,)
                ).fetchone()
                if row:
                    base_items.append({"id": entity_id, "content_hash": row["content_hash"]})
        finally:
            conn.close()

    proposal_data: dict[str, Any] = {
        "proposal": proposal_id,
        "agent": "identity.emit_candidates_proposal",
        "created_at": now,
        "rationale": (
            "These participants could not be auto-resolved during import. "
            "Review the candidate matches and accept or reject each alias."
        ),
        "confidence": "low",
        "evidence": evidence_items,
        "base": base_items,
        "ops": ops,
    }

    out_path = pending_dir / f"identity-unresolved-{proposal_id}.yaml"
    out_path.write_text(
        yaml.safe_dump(proposal_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out_path
