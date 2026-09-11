"""Tests for synapse.dossier — company/person dossier assembly (G2).

Extends the fixture vault (tests/fixtures/vault) with a small set of
additional, hand-authored entities scoped to this test module only. The
shared fixture is never mutated in place: it is copied into an isolated
tmp_path vault (same pattern as test_brief.py / test_mcp_format.py), then a
handful of new markdown entity files are written into that copy before
reindexing.

New scenario entities (ids chosen to avoid any collision with the shared
fixture's 01J0...0001-0013 range — see `entities` grep before editing):
  - 01J00000000000000000000021  Dossier Former Contact (person, proposed) —
    former_employee_of Example Company (01J...0002), window 2020-01..2022-06.
  - 01J00000000000000000000022  Dossier Opportunity At Example Company
    (opportunity) — targets Example Company.
  - 01J00000000000000000000023  Dossier Recruiter (person) — recruits_for
    Example Company directly (real-vault confirmed direction, not via an
    opportunity intermediary).
  - 01J00000000000000000000024  Dossier Chat About Example Company
    (conversation) — body wikilinks [[Example Company]], producing a weak
    `mentioned_in` edge conversation -> company.
  - 01J00000000000000000000025  Dossier Meetup For Example Company (event) —
    same weak-link mechanism, event -> company.
  - 01J00000000000000000000030  Dossier Target Person (person, proposed) —
    the PERSON dossier's subject: works_at Example Company, met_at the
    existing Example Career Meetup event (01J...0010), knows `me`,
    demonstrates_skill the existing Example Skill (01J...0009), and
    introduced_by Dossier Introducer (01J...0033) — i.e. an `introduced_by`
    edge that does NOT target `me`, regression coverage for FIX-20 (the
    person dossier's SS4 previously dropped any relation whose *type* was in
    a fixed "already shown" set, even when that particular edge was never
    actually rendered by SS2/SS3 because it didn't connect to `me`).
  - 01J00000000000000000000031 / 032  two conversations participated_in by
    the target person, dated 2026-06-05 and 2026-01-01 (newest-first sort
    check).
  - 01J00000000000000000000033  Dossier Introducer (person, verified) — the
    peer on the target person's `introduced_by` edge; has no other relations.
  - `me` (rewritten in place, in the tmp copy only) gains: former_employee_of
    Example Company (2019-01..2021-01, overlapping the former contact's
    window), attended the existing Example Career Meetup event (shared-venue
    check), has_interaction with the target person.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.dossier import (
    DOSSIER_HEADER,
    build_company_dossier,
    build_dossier,
    build_person_dossier,
)
from synapse.index import connect, reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

COMPANY_ID = "01J00000000000000000000002"  # Example Company
SKILL_ID = "01J00000000000000000000009"  # Example Skill
EVENT_ID = "01J00000000000000000000010"  # Example Career Meetup
CURRENT_CONTACT_ID = "01J00000000000000000000001"  # Example Person

FORMER_CONTACT_ID = "01J00000000000000000000021"
OPPORTUNITY_ID = "01J00000000000000000000022"
RECRUITER_ID = "01J00000000000000000000023"
COMPANY_CONV_ID = "01J00000000000000000000024"
COMPANY_EVENT_ID = "01J00000000000000000000025"
TARGET_PERSON_ID = "01J00000000000000000000030"
CONV_NEW_ID = "01J00000000000000000000031"
CONV_OLD_ID = "01J00000000000000000000032"
INTRODUCER_ID = "01J00000000000000000000033"
COMPANY_INSIGHT_ID = "01J00000000000000000000034"

_ME_MD = """---
id: me
type: person
name: Me
aliases: []
review_status: verified
tags:
  - owner
relations:
  - type: has_goal
    target: 01J00000000000000000000005
    properties: {}
    source: test-fixture
  - type: former_employee_of
    target: 01J00000000000000000000002
    properties:
      started_on: "2019-01"
      finished_on: "2021-01"
    source: test-dossier
  - type: attended
    target: 01J00000000000000000000010
    properties: {}
    source: test-dossier
  - type: has_interaction
    target: 01J00000000000000000000030
    properties: {}
    source: test-dossier
  - type: has_interaction
    target: 01J00000000000000000000001
    properties:
      note: Discussed the target company directly
    source: owner-recall
properties:
  source:
    - fixture
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Me

Owner entity for this fixture vault.
"""

_FORMER_CONTACT_MD = """---
id: 01J00000000000000000000021
type: person
name: Dossier Former Contact
aliases: []
review_status: proposed
tags: []
relations:
  - type: former_employee_of
    target: 01J00000000000000000000002
    properties:
      started_on: "2020-01"
      finished_on: "2022-06"
    source: test-dossier
properties:
  current_company: Somewhere Else
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Former Contact

Fictional former contact used for dossier tests.
"""

_OPPORTUNITY_MD = """---
id: 01J00000000000000000000022
type: opportunity
name: Dossier Opportunity At Example Company
aliases: []
review_status: verified
tags: []
relations:
  - type: targets
    target: 01J00000000000000000000002
    properties: {}
    source: test-dossier
properties:
  status: prospect
  company: Example Company
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Opportunity At Example Company

Fictional opportunity used for dossier tests.
"""

_RECRUITER_MD = """---
id: 01J00000000000000000000023
type: person
name: Dossier Recruiter
aliases: []
review_status: verified
tags:
  - recruiter
relations:
  - type: recruits_for
    target: 01J00000000000000000000002
    properties: {}
    source: test-dossier
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Recruiter

Fictional recruiter used for dossier tests.
"""

_COMPANY_CONV_MD = """---
id: 01J00000000000000000000024
type: conversation
name: Dossier Chat About Example Company
aliases: []
review_status: verified
tags: []
relations: []
properties:
  date: "2026-06-01"
  channel: email
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Chat About Example Company

Talked about [[Example Company]] today.
"""

_COMPANY_EVENT_MD = """---
id: 01J00000000000000000000025
type: event
name: Dossier Meetup For Example Company
aliases: []
review_status: verified
tags: []
relations: []
properties:
  date: "2026-05-15"
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Meetup For Example Company

Met folks connected to [[Example Company]].
"""

_TARGET_PERSON_MD = """---
id: 01J00000000000000000000030
type: person
name: Dossier Target Person
aliases: []
review_status: proposed
tags: []
relations:
  - type: works_at
    target: 01J00000000000000000000002
    properties:
      role: Analyst
      since: "2023"
    source: test-dossier
  - type: met_at
    target: 01J00000000000000000000010
    properties: {}
    source: test-dossier
  - type: knows
    target: me
    properties: {}
    source: test-dossier
  - type: demonstrates_skill
    target: 01J00000000000000000000009
    properties: {}
    source: test-dossier
  - type: introduced_by
    target: 01J00000000000000000000033
    properties:
      note: Introduced at a conference dinner
    source: owner-recall
properties:
  hometown: Testland
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Target Person

Fictional dossier subject.
"""

_INTRODUCER_MD = """---
id: 01J00000000000000000000033
type: person
name: Dossier Introducer
aliases: []
review_status: verified
tags: []
relations: []
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Introducer

Fictional introducer used for dossier tests (FIX-20 regression: the target
person's `introduced_by` edge points here, not at `me`).
"""

_CONV_NEW_MD = """---
id: 01J00000000000000000000031
type: conversation
name: Dossier Conversation Newer
aliases: []
review_status: verified
tags: []
relations:
  - type: participated_in
    target: 01J00000000000000000000030
    properties: {}
    source: test-dossier
properties:
  date: "2025-01-01"
  last_message_at: "2026-06-05T09:00:00Z"
  channel: call
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Conversation Newer

Fictional newer conversation used for dossier tests.
"""

_CONV_OLD_MD = """---
id: 01J00000000000000000000032
type: conversation
name: Dossier Conversation Older
aliases: []
review_status: verified
tags: []
relations:
  - type: participated_in
    target: 01J00000000000000000000030
    properties: {}
    source: test-dossier
properties:
  date: "2026-12-31"
  last_message_at: "2026-01-01T09:00:00Z"
  channel: email
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Conversation Older

Fictional older conversation used for dossier tests.
"""

_COMPANY_INSIGHT_MD = """---
id: 01J00000000000000000000034
type: insight
name: Dossier Strategic Insight
aliases: []
review_status: proposed
tags: []
relations:
  - type: related_to
    target: 01J00000000000000000000002
    properties: {}
    source: test-dossier
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Dossier Strategic Insight

Fictional strategic insight linked to Example Company.
"""


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))

    # Overwrite `me` in place (tmp copy only — never the checked-in fixture).
    (v / "entities" / "people" / "me.md").write_text(_ME_MD, encoding="utf-8")

    # Add new, isolated scenario entities.
    (v / "entities" / "people" / "dossier-former-contact.md").write_text(
        _FORMER_CONTACT_MD, encoding="utf-8"
    )
    (v / "entities" / "opportunities" / "dossier-opportunity.md").write_text(
        _OPPORTUNITY_MD, encoding="utf-8"
    )
    (v / "entities" / "people" / "dossier-recruiter.md").write_text(
        _RECRUITER_MD, encoding="utf-8"
    )
    (v / "entities" / "conversations" / "dossier-company-conversation.md").write_text(
        _COMPANY_CONV_MD, encoding="utf-8"
    )
    (v / "entities" / "events" / "dossier-company-event.md").write_text(
        _COMPANY_EVENT_MD, encoding="utf-8"
    )
    (v / "entities" / "people" / "dossier-target-person.md").write_text(
        _TARGET_PERSON_MD, encoding="utf-8"
    )
    (v / "entities" / "people" / "dossier-introducer.md").write_text(
        _INTRODUCER_MD, encoding="utf-8"
    )
    (v / "entities" / "conversations" / "dossier-conversation-new.md").write_text(
        _CONV_NEW_MD, encoding="utf-8"
    )
    (v / "entities" / "conversations" / "dossier-conversation-old.md").write_text(
        _CONV_OLD_MD, encoding="utf-8"
    )
    (v / "entities" / "insights" / "dossier-company-insight.md").write_text(
        _COMPANY_INSIGHT_MD, encoding="utf-8"
    )

    reindex(v, full=True)
    return v


@pytest.fixture()
def conn(vault: Path):
    c = connect(vault)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Company dossier
# ---------------------------------------------------------------------------


def test_company_dossier_required_sections_present(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    for header in (
        "## 1. Identity",
        "## 2. Current Contacts",
        "## 3. Former Contacts",
        "## 4. Owner's Own History & Overlap",
        "## 5. Conversations, Opportunities & Events",
        "## 6. Recommended Warm Entry Points",
    ):
        assert header in text, f"missing section header: {header}"
    assert text.startswith(DOSSIER_HEADER)


def test_company_dossier_current_and_former_contacts(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    assert f"`{CURRENT_CONTACT_ID}`" in text
    assert f"`{FORMER_CONTACT_ID}`" in text
    assert "Dossier Former Contact" in text
    current_section = text.split("## 2. Current Contacts", 1)[1].split("## 3.", 1)[0]
    assert "direct owner relationship" in current_section
    assert "Discussed the target company directly" in current_section


def test_company_dossier_proposed_labeled_not_dropped(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    # The former contact is review_status: proposed — must be labeled, never filtered out.
    idx = text.find(FORMER_CONTACT_ID)
    assert idx != -1
    nearby = text[max(0, idx - 80) : idx + 20]
    assert "(unverified)" in nearby


def test_company_dossier_owner_overlap(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    section = text.split("## 4. Owner's Own History & Overlap", 1)[1]
    section = section.split("## 5.", 1)[0]
    assert "former employee" in section
    # The former contact's tenure (2020-01..2022-06) overlaps the owner's (2019-01..2021-01).
    assert FORMER_CONTACT_ID in section


def test_company_dossier_activity_section(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    section = text.split("## 5. Conversations, Opportunities & Events", 1)[1]
    section = section.split("## 6.", 1)[0]
    assert OPPORTUNITY_ID in section
    assert "targets" in section
    assert RECRUITER_ID in section
    assert "recruits_for" in section
    assert COMPANY_CONV_ID in section
    assert COMPANY_EVENT_ID in section
    assert "Conversations mentioning this company" in section
    assert "Events mentioning this company" in section
    assert "Related projects and insights" in section
    assert COMPANY_INSIGHT_ID in section


def test_company_dossier_warm_entry_points_ranked(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID)
    section = text.split("## 6. Recommended Warm Entry Points", 1)[1]
    # G3: the section is now rendered by the shared evidence-ranker.
    assert "Evidence-ranked connectors" in section
    assert TARGET_PERSON_ID in section
    assert CURRENT_CONTACT_ID in section
    assert FORMER_CONTACT_ID in section
    # Every ranked row carries a score and explanatory evidence.
    assert "— score " in section
    # The most-connected contact (works at target + has_interaction + shared
    # event) must outrank the current-only and former-only contacts.
    assert section.index(TARGET_PERSON_ID) < section.index(FORMER_CONTACT_ID)


def test_company_dossier_not_a_company_returns_comment(conn) -> None:
    text = build_company_dossier(conn, SKILL_ID)
    assert "not company" in text
    assert "<!--" in text


def test_company_dossier_not_found(conn) -> None:
    text = build_company_dossier(conn, "does-not-exist")
    assert "not found" in text


# ---------------------------------------------------------------------------
# Person dossier
# ---------------------------------------------------------------------------


def test_person_dossier_required_sections_present(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    for header in (
        "## 1. Identity & Role",
        "## 2. Relationship Evidence",
        "## 3. Conversation History",
        "## 4. Everything Else",
    ):
        assert header in text, f"missing section header: {header}"
    assert text.startswith(DOSSIER_HEADER)


def test_person_dossier_identity_role_and_proposed_labeled(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    assert f"`{TARGET_PERSON_ID}`" in text
    assert "(unverified)" in text  # target person itself is review_status: proposed
    assert "Analyst" in text
    assert "Example Company" in text


def test_person_dossier_relationship_evidence(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    section = text.split("## 2. Relationship Evidence", 1)[1]
    section = section.split("## 3.", 1)[0]
    assert "Direct connection to owner" in section
    assert "knows" in section
    assert "`me`" in section
    assert "Shared employers" in section
    assert COMPANY_ID in section
    assert "Shared schools / events" in section
    assert EVENT_ID in section


def test_person_dossier_conversation_history_newest_first(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    section = text.split("## 3. Conversation History", 1)[1]
    section = section.split("## 4.", 1)[0]
    assert "has_interaction" in section
    assert CONV_NEW_ID in section
    assert CONV_OLD_ID in section
    assert section.index(CONV_NEW_ID) < section.index(CONV_OLD_ID)
    assert "2026-06-05" in section
    assert "2026-01-01" in section


def test_person_dossier_everything_else(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    section = text.split("## 4. Everything Else", 1)[1]
    assert "demonstrates_skill" in section
    assert SKILL_ID in section
    assert "(unverified) (unverified)" not in section
    assert "Testland" in section  # location property, not skipped


def test_person_dossier_everything_else_includes_introduced_by(conn) -> None:
    """FIX-20 regression.

    `introduced_by` used to be blanket-excluded from SS4 by relation *type*,
    on the assumption it was always already rendered in SS2's "direct
    connection to owner" block. But SS2 only renders `introduced_by` edges
    between the subject and `me` — an `introduced_by` edge pointing at some
    other person (a relation between two non-owner records, not `me`) was
    silently dropped from the whole dossier.
    """
    text = build_person_dossier(conn, TARGET_PERSON_ID)
    section = text.split("## 4. Everything Else", 1)[1]
    assert "introduced_by" in section
    assert INTRODUCER_ID in section
    assert "Dossier Introducer" in section
    # Provenance (source + note property) must ride along, not just the edge.
    assert "owner-recall" in section
    assert "Introduced at a conference dinner" in section


def test_person_dossier_not_a_person_returns_comment(conn) -> None:
    text = build_person_dossier(conn, COMPANY_ID)
    assert "not person" in text
    assert "<!--" in text


def test_person_dossier_not_found(conn) -> None:
    text = build_person_dossier(conn, "does-not-exist")
    assert "not found" in text


# ---------------------------------------------------------------------------
# Budget cap + truncation
# ---------------------------------------------------------------------------


def test_company_dossier_budget_truncates_low_priority_sections(conn) -> None:
    text = build_company_dossier(conn, COMPANY_ID, budget_tokens=5)
    assert "## 1. Identity" in text  # priority 1 never trimmed
    assert "<!-- truncated:" in text
    assert "## 6. Recommended Warm Entry Points" not in text


def test_person_dossier_budget_truncates_low_priority_sections(conn) -> None:
    text = build_person_dossier(conn, TARGET_PERSON_ID, budget_tokens=5)
    assert "## 1. Identity & Role" in text
    assert "<!-- truncated:" in text
    assert "## 4. Everything Else" not in text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_company_dossier_is_deterministic(conn) -> None:
    a = build_company_dossier(conn, COMPANY_ID)
    b = build_company_dossier(conn, COMPANY_ID)
    assert a == b


def test_person_dossier_is_deterministic(conn) -> None:
    a = build_person_dossier(conn, TARGET_PERSON_ID)
    b = build_person_dossier(conn, TARGET_PERSON_ID)
    assert a == b


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_build_dossier_dispatches_company_and_person(conn) -> None:
    company_text = build_dossier(conn, COMPANY_ID)
    assert "## 1. Identity" in company_text
    person_text = build_dossier(conn, TARGET_PERSON_ID)
    assert "## 1. Identity & Role" in person_text


def test_build_dossier_unsupported_type(conn) -> None:
    text = build_dossier(conn, SKILL_ID)
    assert "dossier surface supports company and person" in text


def test_build_dossier_not_found(conn) -> None:
    text = build_dossier(conn, "does-not-exist")
    assert "not found" in text


# ---------------------------------------------------------------------------
# No provider imports (mirrors test_brief.py's guardrail test)
# ---------------------------------------------------------------------------


def test_dossier_module_has_no_llm_provider_imports() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "synapse" / "dossier.py").read_text(
        encoding="utf-8"
    )
    for banned in ("openai", "anthropic", "requests", "httpx"):
        assert banned not in src.lower(), f"dossier.py must not import {banned}"
