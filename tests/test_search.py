"""Tests for T4.2: Hybrid search — FTS + semantic + RRF fusion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.embeddings import HashEmbedder, embed_entities
from synapse.index import reindex
from synapse.search import hybrid_search

# ---------------------------------------------------------------------------
# Entity fixtures — no real vault data
# ---------------------------------------------------------------------------

_ALICE = """\
---
id: search-test-001
type: person
name: Alice
review_status: proposed
tags: []
relations: []
properties: {}
---
Alice is a person in security.
"""

_BOB = """\
---
id: search-test-002
type: person
name: Bob
review_status: proposed
tags: []
relations: []
properties: {}
---
Bob works in finance.
"""

_ACME = """\
---
id: search-test-003
type: company
name: Acme Corp
review_status: proposed
tags: []
relations: []
properties: {}
---
Acme Corp is a technology company.
"""

_RECRUITER_CONVERSATION = """\
---
id: search-test-004
type: conversation
name: Conversation with Recruiter
review_status: proposed
tags:
- conversation
relations:
- type: participated_in
  target: search-test-001
  direction: incoming
properties: {}
---
I said I was not interested in smart contract roles right now, but asked the
recruiter to keep in touch if they come across cryptography or blockchain
security roles.
"""

_FIREBLOCKS = """\
---
id: search-test-005
type: company
name: Fireblocks
review_status: proposed
tags: []
relations: []
properties: {}
---
Fireblocks builds wallet and digital asset custody infrastructure.
"""

_FORDEFI = """\
---
id: search-test-006
type: company
name: FORDEFI
review_status: proposed
tags: []
relations: []
properties: {}
---
FORDEFI builds institutional MPC wallet infrastructure.
"""

_TALOS = """\
---
id: search-test-007
type: company
name: Talos
review_status: proposed
tags: []
relations: []
properties: {}
---
Talos is relevant to digital asset trading and custody workflows.
"""

_GO_RECOMMENDER = """\
---
id: search-test-008
type: person
name: Recommender
review_status: proposed
tags: []
relations: []
properties: {}
---
LinkedIn recommendation for threshold cryptography, GoLang, Solidity, and collaboration.
"""

_ONGOING_CONTACT = """\
---
id: search-test-009
type: person
name: Ongoing Contact
review_status: proposed
tags: []
relations: []
properties: {}
---
An ongoing professional connection.
"""


@pytest.fixture()
def temp_vault(tmp_path: Path) -> Path:
    """Minimal vault with 3 entity Markdown files, reindexed."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".synapse").mkdir()
    people_dir = vault / "entities" / "people"
    people_dir.mkdir(parents=True)
    companies_dir = vault / "entities" / "companies"
    companies_dir.mkdir(parents=True)

    (people_dir / "alice.md").write_text(_ALICE, encoding="utf-8")
    (people_dir / "bob.md").write_text(_BOB, encoding="utf-8")
    (companies_dir / "acme.md").write_text(_ACME, encoding="utf-8")

    reindex(vault)
    return vault


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_text_only_returns_results(temp_vault: Path) -> None:
    """text_only search for 'alice' should find the Alice entity."""
    result = hybrid_search(temp_vault, "alice", text_only=True, embedder=HashEmbedder())
    assert isinstance(result["results"], list)
    assert len(result["results"]) > 0
    names = [r["name"] for r in result["results"]]
    assert any("Alice" in name for name in names)


def test_search_refuses_hash_embedder(temp_vault: Path) -> None:
    """Without text_only, HashEmbedder should cause semantic leg refusal."""
    result = hybrid_search(temp_vault, "alice", embedder=HashEmbedder())
    assert result["semantic"].startswith("refused:hash-fallback")


def test_search_zero_hit_logs(temp_vault: Path) -> None:
    """Query with no match should return empty results list."""
    result = hybrid_search(temp_vault, "zzz_nomatch_xyz", text_only=True, embedder=HashEmbedder())
    assert result["results"] == []


def test_search_rrf_score(temp_vault: Path) -> None:
    """Results should have a positive score and be sorted descending."""
    result = hybrid_search(temp_vault, "alice", text_only=True, embedder=HashEmbedder())
    items = result["results"]
    assert len(items) > 0
    for item in items:
        assert item["score"] > 0.0
    # If more than one result, scores should be non-increasing
    scores = [item["score"] for item in items]
    assert scores == sorted(scores, reverse=True)


def test_search_result_structure(temp_vault: Path) -> None:
    """Each result dict must contain the required keys."""
    result = hybrid_search(temp_vault, "alice", text_only=True, embedder=HashEmbedder())
    for item in result["results"]:
        for key in ("id", "name", "type", "snippet", "legs", "score"):
            assert key in item, f"Missing key {key!r} in result: {item}"


def test_search_model_mismatch_refused(temp_vault: Path) -> None:
    """After embedding with HashEmbedder, a different-model embedder should be refused."""
    embed_entities(temp_vault, embedder=HashEmbedder())

    class FakeEmbedder:
        model = "fake-model"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 8 for _ in texts]

    result = hybrid_search(temp_vault, "alice", embedder=FakeEmbedder())
    assert result["semantic"].startswith("refused:model-mismatch")


def test_search_embedding_timeout_returns_text_results(temp_vault: Path) -> None:
    class StoredEmbedder:
        model = "timeout-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    class TimedOutEmbedder:
        model = "timeout-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise TimeoutError("injected timeout")

    embed_entities(temp_vault, embedder=StoredEmbedder(), force_all=True)
    result = hybrid_search(temp_vault, "alice", embedder=TimedOutEmbedder())

    assert result["semantic"] == "refused:timeout"
    assert result["results"][0]["id"] == "search-test-001"
    assert result["results"][0]["legs"] == ["text"]


def test_search_vector_index_error_returns_text_results(temp_vault: Path) -> None:
    class StoredEmbedder:
        model = "index-error-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    class BrokenIndex:
        def search(self, *args: object, **kwargs: object) -> list[dict]:
            raise ValueError("injected malformed snapshot")

    embedder = StoredEmbedder()
    embed_entities(temp_vault, embedder=embedder, force_all=True)
    result = hybrid_search(
        temp_vault,
        "alice",
        embedder=embedder,
        semantic_index=BrokenIndex(),
    )

    assert result["semantic"] == "refused:index-error"
    assert result["results"][0]["id"] == "search-test-001"


def test_search_text_only_recalls_partial_matches_for_long_questions(temp_vault: Path) -> None:
    """Long natural-language questions should not require every term to match one entity."""
    conversations_dir = temp_vault / "entities" / "conversations"
    conversations_dir.mkdir(parents=True)
    (conversations_dir / "recruiter.md").write_text(
        _RECRUITER_CONVERSATION,
        encoding="utf-8",
    )
    reindex(temp_vault)

    result = hybrid_search(
        temp_vault,
        "not interested keep in touch come across cryptography blockchain security core protocol recruiter",
        text_only=True,
        embedder=HashEmbedder(),
        limit=5,
    )

    ids = [item["id"] for item in result["results"]]
    assert "search-test-004" in ids


def test_search_can_limit_discovery_to_conversations(temp_vault: Path) -> None:
    conversations_dir = temp_vault / "entities" / "conversations"
    conversations_dir.mkdir(parents=True)
    (conversations_dir / "recruiter.md").write_text(
        _RECRUITER_CONVERSATION,
        encoding="utf-8",
    )
    reindex(temp_vault)

    result = hybrid_search(
        temp_vault,
        "cryptography recruiter",
        text_only=True,
        entity_type="conversation",
    )

    assert [item["id"] for item in result["results"]] == ["search-test-004"]
    assert result["results"][0]["participants"] == [
        {"id": "search-test-001", "name": "Alice"}
    ]


def test_search_normalizes_plural_entity_type(temp_vault: Path) -> None:
    conversations_dir = temp_vault / "entities" / "conversations"
    conversations_dir.mkdir(parents=True)
    (conversations_dir / "recruiter.md").write_text(
        _RECRUITER_CONVERSATION,
        encoding="utf-8",
    )
    reindex(temp_vault)

    result = hybrid_search(
        temp_vault,
        "cryptography recruiter",
        text_only=True,
        entity_type="conversations",
    )

    assert [item["id"] for item in result["results"]] == ["search-test-004"]


def test_search_treats_go_as_language_alias_not_substring(temp_vault: Path) -> None:
    people_dir = temp_vault / "entities" / "people"
    (people_dir / "recommender.md").write_text(_GO_RECOMMENDER, encoding="utf-8")
    (people_dir / "ongoing.md").write_text(_ONGOING_CONTACT, encoding="utf-8")
    reindex(temp_vault)

    result = hybrid_search(
        temp_vault,
        "Go",
        text_only=True,
        entity_type="person",
    )

    assert result["results"][0]["id"] == "search-test-008"
    assert "search-test-009" not in {item["id"] for item in result["results"]}


def test_search_text_only_recalls_named_companies_from_broad_target_query(
    temp_vault: Path,
) -> None:
    """A broad company-targeting query should recall named companies independently."""
    companies_dir = temp_vault / "entities" / "companies"
    (companies_dir / "fireblocks.md").write_text(_FIREBLOCKS, encoding="utf-8")
    (companies_dir / "fordefi.md").write_text(_FORDEFI, encoding="utf-8")
    (companies_dir / "talos.md").write_text(_TALOS, encoding="utf-8")
    reindex(temp_vault)

    result = hybrid_search(
        temp_vault,
        "custody MPC wallet key management companies Anchorage Coinbase Fireblocks FORDEFI Talos",
        text_only=True,
        embedder=HashEmbedder(),
        limit=10,
    )

    ids = {item["id"] for item in result["results"]}
    assert {"search-test-005", "search-test-006", "search-test-007"} <= ids


def test_search_hybrid_keeps_strong_text_matches_above_semantic_noise(
    temp_vault: Path,
) -> None:
    """Semantic expansion should not push strong indexed text evidence out of view."""
    query = (
        "not interested keep in touch come across cryptography blockchain "
        "security core protocol recruiter"
    )

    conversations_dir = temp_vault / "entities" / "conversations"
    opportunities_dir = temp_vault / "entities" / "opportunities"
    conversations_dir.mkdir(parents=True)
    opportunities_dir.mkdir(parents=True)

    for index in range(12):
        (conversations_dir / f"text-noise-{index}.md").write_text(
            f"""\
---
id: search-text-noise-{index:02d}
type: conversation
name: Text Noise {index}
review_status: proposed
tags:
- conversation
relations: []
properties: {{}}
---
not interested keep in touch come across cryptography blockchain security core protocol recruiter recruiter
""",
            encoding="utf-8",
        )

    (conversations_dir / "exact-soft-reject.md").write_text(
        """\
---
id: search-test-008
type: conversation
name: Exact Soft Reject
review_status: proposed
tags:
- conversation
relations: []
properties: {}
---
I was not interested then, but asked the recruiter to keep in touch for
cryptography or blockchain security roles.
""",
        encoding="utf-8",
    )

    for index in range(30):
        (opportunities_dir / f"semantic-noise-{index}.md").write_text(
            f"""\
---
id: search-semantic-noise-{index:02d}
type: opportunity
name: Semantic Noise {index}
review_status: proposed
tags: []
relations: []
properties: {{}}
---
semantic-only-noise marker {index}
""",
            encoding="utf-8",
        )

    class NoisySemanticEmbedder:
        model = "noisy-semantic-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                if text == query or "semantic-only-noise" in text:
                    vectors.append([1.0, 0.0])
                else:
                    vectors.append([0.0, 1.0])
            return vectors

    embedder = NoisySemanticEmbedder()
    embed_entities(temp_vault, embedder=embedder, force_all=True)

    result = hybrid_search(temp_vault, query, embedder=embedder, limit=15)

    ids = [item["id"] for item in result["results"]]
    assert "search-test-008" in ids
