"""Tests for synapse.warmpath — the shared evidence-ranked connector scorer (G3).

Same isolation discipline as test_dossier.py: the checked-in fixture vault is
copied into tmp_path and only the copy is mutated. Scenario entities use ids in
the 01J0...0040-0052 range to avoid any collision with the shared fixture
(0001-0013) or test_dossier's own scenario (0021-0032).

Scenario (all fictional):
  - 01J...0050  WP Company (company) — the company target.
  - 01J...0051  WP Event (event) — a shared venue.
  - 01J...0052  WP Conversation (conversation, 30 msgs, 2026-06-20) —
    participated in by the owner and WP Friend.
  - 01J...0040  WP Target Person (person) — the person target: works_at WP
    Company (since 2022), attended WP Event, location Berlin.
  - 01J...0041  WP Colleague — works_at WP Company (since 2023): shares an
    employer with the target (overlapping tenure).
  - 01J...0042  WP Alum — attended WP Event: shares a venue with the target
    AND with the owner.
  - 01J...0043  WP Friend — knows the target directly, is in a deep recent
    conversation with the owner, and endorsed the owner's skill.
  - `me` (rewritten in the copy) — attended WP Event, participated_in WP
    Conversation, has_interaction with WP Friend.
"""

from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.dossier import build_company_dossier
from synapse.index import connect, reindex
from synapse.warmpath import rank_connectors

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

# Deterministic "now" so the recency tiers never drift with the wall clock.
TODAY = date(2026, 7, 6)

WP_COMPANY_ID = "01J00000000000000000000050"
WP_EVENT_ID = "01J00000000000000000000051"
WP_CONV_ID = "01J00000000000000000000052"
WP_TARGET_ID = "01J00000000000000000000040"
WP_COLLEAGUE_ID = "01J00000000000000000000041"
WP_ALUM_ID = "01J00000000000000000000042"
WP_FRIEND_ID = "01J00000000000000000000043"

_ME_MD = """---
id: me
type: person
name: Me
aliases: []
review_status: verified
tags:
  - owner
relations:
  - type: attended
    target: 01J00000000000000000000051
    properties: {}
    source: test-warmpath
  - type: participated_in
    target: 01J00000000000000000000052
    properties: {}
    source: test-warmpath
  - type: has_interaction
    target: 01J00000000000000000000043
    properties: {}
    source: test-warmpath
properties:
  source:
    - fixture
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# Me

Owner entity for this fixture vault.
"""

_WP_COMPANY_MD = """---
id: 01J00000000000000000000050
type: company
name: WP Company
aliases: []
review_status: verified
tags: []
relations: []
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Company

Fictional company used for warm-path tests.
"""

_WP_EVENT_MD = """---
id: 01J00000000000000000000051
type: event
name: WP Event
aliases: []
review_status: verified
tags: []
relations: []
properties:
  date: "2026-05-01"
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Event

Fictional event used for warm-path tests.
"""

_WP_CONV_MD = """---
id: 01J00000000000000000000052
type: conversation
name: WP Conversation
aliases: []
review_status: verified
tags: []
relations: []
properties:
  date: "2026-06-20"
  last_message_at: "2026-06-20 09:00:00 UTC"
  message_count: 30
  channel: chat
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Conversation

Fictional conversation used for warm-path tests.
"""

_WP_TARGET_MD = """---
id: 01J00000000000000000000040
type: person
name: WP Target Person
aliases: []
review_status: proposed
tags: []
relations:
  - type: works_at
    target: 01J00000000000000000000050
    properties:
      role: Engineer
      since: "2022"
    source: test-warmpath
  - type: attended
    target: 01J00000000000000000000051
    properties: {}
    source: test-warmpath
properties:
  location: Berlin
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Target Person

Fictional warm-path target.
"""

_WP_COLLEAGUE_MD = """---
id: 01J00000000000000000000041
type: person
name: WP Colleague
aliases: []
review_status: proposed
tags: []
relations:
  - type: works_at
    target: 01J00000000000000000000050
    properties:
      role: Engineer
      since: "2023"
    source: test-warmpath
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Colleague

Fictional colleague of the target.
"""

_WP_ALUM_MD = """---
id: 01J00000000000000000000042
type: person
name: WP Alum
aliases: []
review_status: proposed
tags: []
relations:
  - type: attended
    target: 01J00000000000000000000051
    properties: {}
    source: test-warmpath
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Alum

Fictional co-attendee of the target's event.
"""

_WP_FRIEND_MD = """---
id: 01J00000000000000000000043
type: person
name: WP Friend
aliases: []
review_status: verified
tags: []
relations:
  - type: knows
    target: 01J00000000000000000000040
    properties: {}
    source: test-warmpath
  - type: participated_in
    target: 01J00000000000000000000052
    properties: {}
    source: test-warmpath
  - type: endorsed_skill
    target: me
    properties:
      skill_name: Cryptography
    source: test-warmpath
properties: {}
created_at: 2026-06-08T10:00:00Z
updated_at: 2026-06-08T10:00:00Z
---

# WP Friend

Fictional direct connection to the target.
"""

_WP_EXCLUSION_MD = f"""---
id: 01J00000000000000000000053
type: insight
name: WP Outreach Exclusion
aliases: []
review_status: verified
tags: []
relations: []
properties:
  excluded_outreach_person_ids:
    - {WP_FRIEND_ID}
created_at: 2026-07-01T00:00:00Z
updated_at: 2026-07-01T00:00:00Z
---

# WP Outreach Exclusion

Fictional owner policy used for warm-path tests.
"""


def _write_scenario(v: Path) -> None:
    (v / "entities" / "people" / "me.md").write_text(_ME_MD, encoding="utf-8")
    (v / "entities" / "companies" / "wp-company.md").write_text(_WP_COMPANY_MD, encoding="utf-8")
    (v / "entities" / "events" / "wp-event.md").write_text(_WP_EVENT_MD, encoding="utf-8")
    (v / "entities" / "conversations" / "wp-conversation.md").write_text(_WP_CONV_MD, encoding="utf-8")
    (v / "entities" / "people" / "wp-target.md").write_text(_WP_TARGET_MD, encoding="utf-8")
    (v / "entities" / "people" / "wp-colleague.md").write_text(_WP_COLLEAGUE_MD, encoding="utf-8")
    (v / "entities" / "people" / "wp-alum.md").write_text(_WP_ALUM_MD, encoding="utf-8")
    (v / "entities" / "people" / "wp-friend.md").write_text(_WP_FRIEND_MD, encoding="utf-8")


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    _write_scenario(v)
    reindex(v, full=True)
    return v


@pytest.fixture()
def conn(vault: Path):
    c = connect(vault)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Person target
# ---------------------------------------------------------------------------

def test_person_target_returns_ranked_connectors(conn) -> None:
    ranked = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    ids = [c["id"] for c in ranked]
    assert WP_FRIEND_ID in ids
    assert WP_COLLEAGUE_ID in ids
    assert WP_ALUM_ID in ids
    # The target itself and the owner are never candidates.
    assert WP_TARGET_ID not in ids
    assert "me" not in ids


def test_person_target_direct_connection_ranks_first(conn) -> None:
    ranked = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    # WP Friend has the direct connection + deep recent conversation + endorsement.
    assert ranked[0]["id"] == WP_FRIEND_ID


def test_every_candidate_has_evidence(conn) -> None:
    """HARD INVARIANT: no candidate without an evidence line."""
    for target in (WP_TARGET_ID, WP_COMPANY_ID):
        ranked = rank_connectors(conn, target, limit=25, today=TODAY)
        assert ranked, f"expected candidates for {target}"
        for cand in ranked:
            assert cand["evidence"], f"{cand['id']} has an empty score with no evidence"
            assert all(isinstance(e, str) and e for e in cand["evidence"])


def test_shared_employer_overlap_bonus_fires(conn) -> None:
    ranked = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    colleague = next(c for c in ranked if c["id"] == WP_COLLEAGUE_ID)
    joined = " ".join(colleague["evidence"]).lower()
    assert "shared employer" in joined
    assert "overlaps" in joined  # tenure overlap bonus


def test_recorded_and_unrecorded_interactions_score_separately(conn) -> None:
    ranked = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    friend = next(c for c in ranked if c["id"] == WP_FRIEND_ID)
    assert any("recorded conversation" in e for e in friend["evidence"])
    assert any("without a recorded transcript" in e for e in friend["evidence"])


def test_stable_rank_order(conn) -> None:
    a = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    b = rank_connectors(conn, WP_TARGET_ID, limit=10, today=TODAY)
    assert [(c["id"], c["score"]) for c in a] == [(c["id"], c["score"]) for c in b]


def test_owner_policy_exclusion_suppresses_warm_path_candidate(vault: Path) -> None:
    (vault / "entities" / "insights" / "wp-exclusion.md").write_text(
        _WP_EXCLUSION_MD, encoding="utf-8"
    )
    reindex(vault, full=True)
    c = connect(vault)
    try:
        ids = [item["id"] for item in rank_connectors(c, WP_TARGET_ID, limit=10, today=TODAY)]
    finally:
        c.close()
    assert WP_FRIEND_ID not in ids


# ---------------------------------------------------------------------------
# Company target
# ---------------------------------------------------------------------------

def test_company_target_returns_employees(conn) -> None:
    ranked = rank_connectors(conn, WP_COMPANY_ID, limit=10, today=TODAY)
    ids = [c["id"] for c in ranked]
    assert WP_TARGET_ID in ids  # currently works at the company
    assert WP_COLLEAGUE_ID in ids
    # Every company-target candidate is an entry point via employment.
    for cand in ranked:
        joined = " ".join(cand["evidence"]).lower()
        assert "works at the target" in joined


def test_company_target_current_employee_evidence(conn) -> None:
    ranked = rank_connectors(conn, WP_COMPANY_ID, limit=10, today=TODAY)
    colleague = next(c for c in ranked if c["id"] == WP_COLLEAGUE_ID)
    assert any("currently works at the target" in e for e in colleague["evidence"])


# ---------------------------------------------------------------------------
# Monotonicity
# ---------------------------------------------------------------------------

def test_monotonicity_adding_signal_raises_score(vault: Path) -> None:
    c1 = connect(vault)
    try:
        before = {c["id"]: c["score"] for c in rank_connectors(c1, WP_COMPANY_ID, limit=25, today=TODAY)}
    finally:
        c1.close()
    base_score = before[WP_COLLEAGUE_ID]

    # Add a fresh signal to WP Colleague: an endorsement of the owner's skill.
    augmented = _WP_COLLEAGUE_MD.replace(
        """    source: test-warmpath
properties: {}""",
        """    source: test-warmpath
  - type: endorsed_skill
    target: me
    properties:
      skill_name: Cryptography
    source: test-warmpath
properties: {}""",
    )
    assert augmented != _WP_COLLEAGUE_MD  # guard: the anchor actually matched
    (vault / "entities" / "people" / "wp-colleague.md").write_text(augmented, encoding="utf-8")
    reindex(vault, full=True)

    c2 = connect(vault)
    try:
        after = {c["id"]: c["score"] for c in rank_connectors(c2, WP_COMPANY_ID, limit=25, today=TODAY)}
    finally:
        c2.close()

    assert after[WP_COLLEAGUE_ID] > base_score
    # Monotonic: no other candidate's score dropped as a side effect.
    for cid, score in before.items():
        if cid == WP_COLLEAGUE_ID:
            continue
        assert after.get(cid, score) >= score


# ---------------------------------------------------------------------------
# Dossier wiring
# ---------------------------------------------------------------------------

def test_dossier_warm_entry_section_uses_ranker(conn) -> None:
    text = build_company_dossier(conn, WP_COMPANY_ID)
    section = text.split("## 6. Recommended Warm Entry Points", 1)[1]
    assert "Evidence-ranked connectors" in section
    assert WP_TARGET_ID in section
    assert "— score " in section
    # id spans are rendered in backticks for citation.
    assert f"`{WP_TARGET_ID}`" in section
