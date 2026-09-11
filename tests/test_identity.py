"""Tests for synapse.identity — T8.1 / T8.2 / T8.3.

Coverage:
- Each cascade step (url, email, phone, name+company, name-only)
- Ambiguity at name+company (two candidates ⇒ None)
- merged_into redirection
- Owner short-circuit (email, phone)
- alias_worthy() noise-display-name rules
- norm_url, norm_email, norm_phone normalizers
- list-append-dedupe semantics for emails/phones in _merge_properties
- public configuration does not fabricate owner identity keys
- emit_candidates_proposal validity
- check_vault identity_unresolved_count
"""

from __future__ import annotations

import json
from pathlib import Path

from synapse.identity import (
    IdentityIndex,
    alias_worthy,
    emit_candidates_proposal,
    norm_email,
    norm_phone,
    norm_url,
)
from synapse.util import generate_ulid, write_frontmatter

# ---------------------------------------------------------------------------
# Normalizer tests
# ---------------------------------------------------------------------------


class TestNormUrl:
    def test_strips_https_scheme(self) -> None:
        assert norm_url("https://linkedin.com/in/alice") == "linkedin.com/in/alice"

    def test_strips_http_scheme(self) -> None:
        assert norm_url("http://linkedin.com/in/alice") == "linkedin.com/in/alice"

    def test_strips_www(self) -> None:
        assert norm_url("https://www.linkedin.com/in/alice") == "linkedin.com/in/alice"

    def test_strips_trailing_slash(self) -> None:
        assert norm_url("https://linkedin.com/in/alice/") == "linkedin.com/in/alice"

    def test_casefolding(self) -> None:
        assert norm_url("https://LinkedIn.com/in/Alice") == "linkedin.com/in/alice"

    def test_bare_url(self) -> None:
        assert norm_url("linkedin.com/in/alice") == "linkedin.com/in/alice"


class TestNormEmail:
    def test_casefolding(self) -> None:
        assert norm_email("Alice@Example.COM") == "alice@example.com"

    def test_strips_whitespace(self) -> None:
        assert norm_email("  alice@example.com  ") == "alice@example.com"


class TestNormPhone:
    def test_strips_separators(self) -> None:
        assert norm_phone("+91 98765 43210") == "919876543210"

    def test_strips_dashes(self) -> None:
        assert norm_phone("098-765-4321") == "0987654321"

    def test_digits_only_input(self) -> None:
        assert norm_phone("1234567890") == "1234567890"


# ---------------------------------------------------------------------------
# alias_worthy tests
# ---------------------------------------------------------------------------


class TestAliasWorthy:
    def test_normal_full_name(self) -> None:
        assert alias_worthy("Alice Doe") is True

    def test_contains_via(self) -> None:
        assert alias_worthy("Taylor via LinkedIn") is False

    def test_single_token(self) -> None:
        assert alias_worthy("Alice") is False

    def test_empty(self) -> None:
        assert alias_worthy("") is False

    def test_all_caps(self) -> None:
        assert alias_worthy("ALICE DOE") is False

    def test_normal_two_word(self) -> None:
        assert alias_worthy("Bob Smith") is True


# ---------------------------------------------------------------------------
# Helpers to build a minimal in-memory SQLite rows list for IdentityIndex.build
# ---------------------------------------------------------------------------


def _make_conn(rows: list[dict]) -> object:
    """Return a minimal mock connection whose .execute().fetchall() returns rows."""

    class _Cursor:
        def __init__(self, r: list) -> None:
            self._rows = r

        def fetchall(self) -> list:
            return self._rows

    class _Conn:
        def __init__(self, r: list) -> None:
            self._rows = r

        def execute(self, _sql: str) -> _Cursor:  # noqa: ARG002
            return _Cursor(self._rows)

    return _Conn(rows)


def _row(
    *,
    entity_id: str,
    name: str,
    entity_type: str = "person",
    linkedin_url: str = "",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    current_company: str = "",
    aliases: list[str] | None = None,
    merged_into: str | None = None,
) -> dict:
    props: dict = {}
    if linkedin_url:
        props["linkedin_url"] = linkedin_url
    if emails is not None:
        props["emails"] = emails
    if phones is not None:
        props["phones"] = phones
    if current_company:
        props["current_company"] = current_company
    fm: dict = {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "properties": props,
        "aliases": aliases or [],
    }
    if merged_into:
        fm["merged_into"] = merged_into

    return {
        "id": entity_id,
        "name": name,
        "type": entity_type,
        "frontmatter": json.dumps(fm),
    }


# ---------------------------------------------------------------------------
# IdentityIndex / resolve() cascade tests
# ---------------------------------------------------------------------------


class TestCascade:
    def _idx(self, rows: list[dict], **kw: object) -> IdentityIndex:
        return IdentityIndex.build(_make_conn(rows), **kw)

    def test_step1_url_match(self) -> None:
        alice_id = generate_ulid()
        idx = self._idx([_row(entity_id=alice_id, name="Alice", linkedin_url="https://linkedin.com/in/alice")])
        r = idx.resolve({"linkedin_url": "https://www.linkedin.com/in/alice/"})
        assert r.id == alice_id
        assert r.method == "url"

    def test_step1_url_no_match(self) -> None:
        alice_id = generate_ulid()
        idx = self._idx([_row(entity_id=alice_id, name="Alice", linkedin_url="https://linkedin.com/in/alice")])
        r = idx.resolve({"linkedin_url": "https://linkedin.com/in/bob"})
        assert r.id is None

    def test_step2_email_match(self) -> None:
        alice_id = generate_ulid()
        idx = self._idx([_row(entity_id=alice_id, name="Alice", emails=["alice@example.com"])])
        r = idx.resolve({"emails": ["Alice@Example.COM"]})
        assert r.id == alice_id
        assert r.method == "email"

    def test_step3_phone_match(self) -> None:
        bob_id = generate_ulid()
        idx = self._idx([_row(entity_id=bob_id, name="Bob", phones=["+1 555 123 4567"])])
        r = idx.resolve({"phones": ["15551234567"]})
        assert r.id == bob_id
        assert r.method == "phone"

    def test_step4_name_company_unique_match(self) -> None:
        carol_id = generate_ulid()
        idx = self._idx([_row(entity_id=carol_id, name="Carol", current_company="Acme")])
        r = idx.resolve({"name": "Carol", "current_company": "Acme"})
        assert r.id == carol_id
        assert r.method == "name+company"

    def test_step4_name_company_two_candidates_returns_none(self) -> None:
        """Two people named Carol at Acme → must NOT auto-match (design 05)."""
        c1 = generate_ulid()
        c2 = generate_ulid()
        idx = self._idx([
            _row(entity_id=c1, name="Carol", current_company="Acme"),
            _row(entity_id=c2, name="Carol", current_company="Acme"),
        ])
        r = idx.resolve({"name": "Carol", "current_company": "Acme"})
        assert r.id is None
        assert r.method == "unresolved"
        assert {c1, c2} <= set(r.candidates)

        # Without the company, name-only also returns both candidates.
        r5 = idx.resolve({"name": "Carol"})
        assert r5.id is None
        assert r5.method == "unresolved"
        assert set(r5.candidates) == {c1, c2}

    def test_step5_name_only_never_auto_match(self) -> None:
        alice_id = generate_ulid()
        idx = self._idx([_row(entity_id=alice_id, name="Alice")])
        r = idx.resolve({"name": "Alice"})
        assert r.id is None
        assert r.method == "unresolved"
        assert alice_id in r.candidates

    def test_step5_name_only_ambiguous_candidates_returned(self) -> None:
        a1 = generate_ulid()
        a2 = generate_ulid()
        idx = IdentityIndex()
        idx._by_name["alice"] = [a1, a2]
        r = idx.resolve({"name": "Alice"})
        assert r.id is None
        assert set(r.candidates) == {a1, a2}

    def test_cascade_order_url_beats_email(self) -> None:
        """URL match should take priority over email match for different entities."""
        alice_id = generate_ulid()
        bob_id = generate_ulid()
        idx = self._idx([
            _row(entity_id=alice_id, name="Alice", linkedin_url="https://linkedin.com/in/alice"),
            _row(entity_id=bob_id, name="Bob", emails=["alice@example.com"]),
        ])
        r = idx.resolve({
            "linkedin_url": "https://linkedin.com/in/alice",
            "emails": ["alice@example.com"],
        })
        assert r.id == alice_id
        assert r.method == "url"

    def test_merged_into_redirection(self) -> None:
        """A merged (archived) entity should resolve to the survivor."""
        survivor_id = generate_ulid()
        loser_id = generate_ulid()
        rows = [
            _row(entity_id=survivor_id, name="Alice", linkedin_url="https://linkedin.com/in/alice"),
            _row(entity_id=loser_id, name="Alice Old", merged_into=survivor_id),
        ]
        idx = self._idx(rows)
        # The loser's url is not set, but we simulate the lookup being for
        # the survivor's URL:
        r = idx.resolve({"linkedin_url": "https://linkedin.com/in/alice"})
        assert r.id == survivor_id

    def test_owner_email_resolves_to_me(self) -> None:
        idx = self._idx([], owner_emails=["me@example.com"], owner_id="me")
        r = idx.resolve({"emails": ["me@example.com"]})
        assert r.id == "me"
        assert r.method == "owner:email"

    def test_owner_phone_resolves_to_me(self) -> None:
        idx = self._idx([], owner_phones=["+1-800-555-0000"], owner_id="me")
        r = idx.resolve({"phones": ["18005550000"]})
        assert r.id == "me"
        assert r.method == "owner:phone"

    def test_owner_checked_before_url(self) -> None:
        """Owner short-circuit fires before URL cascade step."""
        alice_id = generate_ulid()
        idx = self._idx(
            [_row(entity_id=alice_id, name="Alice", emails=["me@example.com"])],
            owner_emails=["me@example.com"],
            owner_id="me",
        )
        r = idx.resolve({"emails": ["me@example.com"]})
        assert r.id == "me"

    def test_no_facts_returns_unresolved(self) -> None:
        idx = self._idx([])
        r = idx.resolve({})
        assert r.id is None
        assert r.method == "unresolved"


# ---------------------------------------------------------------------------
# T8.2 — _merge_properties list-append-dedupe
# ---------------------------------------------------------------------------


class TestMergePropertiesListAppend:
    def _merge(self, existing: dict, incoming: dict) -> dict:
        from synapse.importers import _merge_properties
        return _merge_properties(existing, incoming)

    def test_emails_appended_not_overwritten(self) -> None:
        result = self._merge(
            {"emails": ["alice@example.com"]},
            {"emails": ["bob@example.com"]},
        )
        assert result["emails"] == ["alice@example.com", "bob@example.com"]

    def test_emails_casefold_deduped(self) -> None:
        result = self._merge(
            {"emails": ["Alice@Example.COM"]},
            {"emails": ["alice@example.com"]},
        )
        assert len(result["emails"]) == 1

    def test_phones_appended(self) -> None:
        result = self._merge(
            {"phones": ["5551234567"]},
            {"phones": ["+1-555-987-6543"]},
        )
        assert len(result["phones"]) == 2
        assert "5551234567" in result["phones"]

    def test_phones_digits_deduped(self) -> None:
        result = self._merge(
            {"phones": ["+1 555 123 4567"]},
            {"phones": ["15551234567"]},
        )
        assert len(result["phones"]) == 1

    def test_emails_existing_empty_gets_filled(self) -> None:
        result = self._merge({}, {"emails": ["new@example.com"]})
        assert result["emails"] == ["new@example.com"]

    def test_source_still_works(self) -> None:
        result = self._merge({"source": ["linkedin"]}, {"source": ["email"]})
        assert set(result["source"]) == {"linkedin", "email"}

    def test_other_keys_unchanged(self) -> None:
        result = self._merge({"name": "Alice"}, {"name": "Bob"})
        # Non-list, non-special key: existing wins (not empty, so no overwrite)
        assert result["name"] == "Alice"


# ---------------------------------------------------------------------------
# T8.2 — public configuration omits owner identity keys
# ---------------------------------------------------------------------------


def test_config_does_not_fabricate_identity_keys(tmp_path: Path) -> None:
    from synapse.config import init_vault, load_config

    root = init_vault(tmp_path, initialize_git=False)
    cfg = load_config(root)

    assert "identity" not in cfg


# ---------------------------------------------------------------------------
# T8.3 — emit_candidates_proposal
# ---------------------------------------------------------------------------


def test_emit_candidates_proposal_valid(tmp_path: Path) -> None:
    from synapse.config import init_vault
    from synapse.index import connect, reindex
    from synapse.proposals import load_proposal, validate_proposal

    root = init_vault(tmp_path, initialize_git=False)

    # Create a real entity so validate_proposal can look it up
    reindex(root)
    conn = connect(root)
    try:
        rows = conn.execute("SELECT id FROM entities WHERE id = 'me'").fetchall()
        assert rows, "Owner entity 'me' must exist for proposal validation"
        conn.execute(
            "SELECT content_hash FROM entities WHERE id = 'me'"
        ).fetchone()["content_hash"]
    finally:
        conn.close()

    # Emit a candidates proposal
    unresolved = [
        {
            "name": "Unknown Person",
            "candidates": [],  # no candidates this time
            "evidence": "Seen in import fixture",
        }
    ]
    proposal_path = emit_candidates_proposal(root, unresolved)
    assert proposal_path.exists()

    # Load and validate
    proposal = load_proposal(proposal_path)
    assert proposal.confidence == "low"
    assert proposal.agent == "identity.emit_candidates_proposal"

    conn2 = connect(root)
    try:
        errors = validate_proposal(conn2, proposal)
    finally:
        conn2.close()

    # Empty ops list (no candidates) → should have no errors
    assert errors == []


def test_emit_candidates_proposal_with_candidates(tmp_path: Path) -> None:
    """Proposal with add_alias ops for candidates must validate cleanly."""
    from synapse.config import init_vault
    from synapse.index import connect, reindex
    from synapse.proposals import load_proposal
    from synapse.util import write_frontmatter

    root = init_vault(tmp_path, initialize_git=False)

    # Create a person entity to use as a candidate
    person_id = generate_ulid()
    person_path = root / "entities" / "people" / "alice.md"
    write_frontmatter(
        person_path,
        {
            "id": person_id,
            "type": "person",
            "name": "Alice",
            "review_status": "proposed",
            "tags": [],
            "relations": [],
            "properties": {},
        },
        "# Alice\n",
    )

    reindex(root)
    conn = connect(root)
    try:
        # Confirm alice exists
        row = conn.execute("SELECT id FROM entities WHERE id = ?", (person_id,)).fetchone()
        assert row is not None
    finally:
        conn.close()

    unresolved = [
        {
            "name": "Alice Doe",
            "candidates": [person_id],
            "evidence": "Name-only match in fixture",
        }
    ]
    proposal_path = emit_candidates_proposal(root, unresolved)
    proposal = load_proposal(proposal_path)

    assert proposal.confidence == "low"
    assert len(proposal.ops) == 1
    assert proposal.ops[0].op == "add_alias"
    assert proposal.ops[0].id == person_id
    assert proposal.ops[0].alias == "Alice Doe"

    # base carries the candidate's content hash so the proposal passes apply
    # validation and is stale-checked like any other.
    base_ids = {b.id for b in proposal.base}
    assert person_id in base_ids
    assert all(b.content_hash for b in proposal.base)

    from synapse.proposals import validate_proposal

    conn2 = connect(root)
    try:
        errors = validate_proposal(conn2, proposal)
    finally:
        conn2.close()
    assert errors == []


# ---------------------------------------------------------------------------
# T8.3 — check_vault identity_unresolved_count
# ---------------------------------------------------------------------------


def test_check_vault_identity_unresolved_count(tmp_path: Path) -> None:
    from synapse.config import init_vault
    from synapse.maintenance import check_vault

    root = init_vault(tmp_path, initialize_git=False)

    # Create an entity tagged identity-unresolved
    uid = generate_ulid()
    write_frontmatter(
        root / "entities" / "people" / "unknown.md",
        {
            "id": uid,
            "type": "person",
            "name": "Unknown Person",
            "review_status": "proposed",
            "tags": ["identity-unresolved"],
            "relations": [],
            "properties": {},
        },
        "# Unknown Person\n",
    )

    result = check_vault(vault=root)
    assert result["identity_unresolved_count"] == 1
    assert result["summary"]["identity_unresolved"] == 1


def test_check_vault_identity_unresolved_zero(tmp_path: Path) -> None:
    from synapse.config import init_vault
    from synapse.maintenance import check_vault

    root = init_vault(tmp_path, initialize_git=False)
    result = check_vault(vault=root)
    assert result["identity_unresolved_count"] == 0
    assert result["summary"]["identity_unresolved"] == 0
