from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

maintenance = pytest.importorskip("synapse.maintenance")


def call_first(*candidate_names):
    for name in candidate_names:
        func = getattr(maintenance, name, None)
        if callable(func):
            return func
    pytest.fail("synapse.maintenance does not expose the expected entry points")


def git_init(repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Digital Synapse Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def invoke_with_variants(func, positional_variants, keyword_variants):
    last_error = None
    for args in positional_variants:
        for kwargs in keyword_variants:
            try:
                return func(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
    raise AssertionError(
        f"unable to call {func.__name__} with known argument variants"
    ) from last_error


def prepare_vault(tmp_path: Path) -> Path:
    fixture_vault = Path(__file__).parent / "fixtures" / "vault"
    vault = tmp_path / "vault"
    import shutil

    shutil.copytree(fixture_vault, vault, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal"))
    git_init(vault)
    return vault


def test_check_reports_dangling_and_duplicate_candidates(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    broken = vault / "entities" / "people" / "broken-person.md"
    broken.write_text(
        """---
id: 01J000000000000000000000AA
type: person
name: Example Person
aliases: [Example P.]
review_status: proposed
relations:
  - type: related_to
    target: 01J00000000000000000000ZZ
    properties: {}
    source: test-fixture
---

# Broken Person
""",
        encoding="utf-8",
    )

    check_vault = call_first("check_vault", "check", "run_check")
    report = invoke_with_variants(
        check_vault,
        [(), (vault,), (str(vault),)],
        [{"vault_path": vault}, {"vault": vault}],
    )

    report_text = str(report)
    assert "related_to" in report_text or "unknown relation" in report_text.lower()
    assert "Example Person" in report_text
    assert "dangling" in report_text.lower() or "unresolved" in report_text.lower()
    assert "orphans" in report


def test_check_accepts_career_relation_types_in_fixture(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    check_vault = call_first("check_vault", "check", "run_check")

    report = invoke_with_variants(
        check_vault,
        [(), (vault,), (str(vault),)],
        [{"vault_path": vault}, {"vault": vault}],
    )

    assert report["unknown_relation_types"] == []


def test_check_does_not_flag_url_distinct_linkedin_people_as_duplicates(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    people_dir = vault / "entities" / "people"
    people_dir.joinpath("shared-name-a.md").write_text(
        """---
id: 01J00000000000000000000AC
type: person
name: Shared Name
aliases: []
review_status: proposed
relations: []
properties:
  linkedin_url: https://www.linkedin.com/in/shared-a
---

# Shared Name
""",
        encoding="utf-8",
    )
    people_dir.joinpath("shared-name-b.md").write_text(
        """---
id: 01J00000000000000000000AD
type: person
name: Shared Name
aliases: []
review_status: proposed
relations: []
properties:
  linkedin_url: https://www.linkedin.com/in/shared-b
---

# Shared Name
""",
        encoding="utf-8",
    )

    check_vault = call_first("check_vault", "check", "run_check")
    report = invoke_with_variants(
        check_vault,
        [(), (vault,), (str(vault),)],
        [{"vault_path": vault}, {"vault": vault}],
    )

    duplicate_names = [[item["name"] for item in group] for group in report["duplicates"]]
    assert ["Shared Name", "Shared Name"] not in duplicate_names


def test_check_dedup_is_type_aware(tmp_path: Path) -> None:
    """FIX-18: a company and a skill sharing a name are NOT duplicate candidates
    (they are a cross-type name collision); two same-type same-name entities are."""
    vault = prepare_vault(tmp_path)
    companies_dir = vault / "entities" / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = vault / "entities" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    companies_dir.joinpath("ethereum.md").write_text(
        """---
id: 01J00000000000000000000EC
type: company
name: Ethereum
aliases: []
review_status: proposed
relations: []
---

# Ethereum
""",
        encoding="utf-8",
    )
    skills_dir.joinpath("ethereum.md").write_text(
        """---
id: 01J00000000000000000000ES
type: skill
name: Ethereum
aliases: []
review_status: proposed
relations: []
---

# Ethereum
""",
        encoding="utf-8",
    )
    # Two genuine same-type same-name skills — still a duplicate candidate.
    skills_dir.joinpath("solidity-a.md").write_text(
        """---
id: 01J00000000000000000000SA
type: skill
name: Solidity
aliases: []
review_status: proposed
relations: []
---

# Solidity
""",
        encoding="utf-8",
    )
    skills_dir.joinpath("solidity-b.md").write_text(
        """---
id: 01J00000000000000000000SB
type: skill
name: Solidity
aliases: []
review_status: proposed
relations: []
---

# Solidity
""",
        encoding="utf-8",
    )

    check_vault = call_first("check_vault", "check", "run_check")
    report = invoke_with_variants(
        check_vault,
        [(), (vault,), (str(vault),)],
        [{"vault_path": vault}, {"vault": vault}],
    )

    duplicate_names = [sorted(item["name"] for item in group) for group in report["duplicates"]]
    # Cross-type Ethereum pair is NOT a duplicate group.
    assert ["Ethereum", "Ethereum"] not in duplicate_names
    # Same-type Solidity pair IS still a duplicate group.
    assert ["Solidity", "Solidity"] in duplicate_names

    # The Ethereum pair surfaces as a softer cross-type name collision instead.
    collisions = report["cross_type_name_collisions"]
    eth = [c for c in collisions if c["name"] == "Ethereum"]
    assert len(eth) == 1
    assert eth[0]["types"] == ["company", "skill"]
    # Same-type collisions (Solidity) are NOT reported as cross-type collisions.
    assert not any(c["name"] == "Solidity" for c in collisions)


def test_merge_rewrites_edges_and_marks_merged_entity(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    duplicate = vault / "entities" / "people" / "example-person-duplicate.md"
    duplicate.write_text(
        """---
id: 01J00000000000000000000AB
type: person
name: Example Person Variant
aliases: [Example Person, EP Variant]
review_status: proposed
relations:
  - type: works_at
    target: 01J00000000000000000000002
    properties: {}
    source: test-fixture
---

# Example Person Variant

Variant notes.
""",
        encoding="utf-8",
    )
    reference = vault / "entities" / "conversations" / "example-conversation.md"
    reference.write_text(
        reference.read_text(encoding="utf-8")
        + "\nSee [[example-person-duplicate|Variant]] and "
        "[[example-person-duplicate#Notes]].\n",
        encoding="utf-8",
    )

    merge_entities = call_first("merge_entities", "merge", "run_merge")
    result = invoke_with_variants(
        merge_entities,
        [
            ("01J00000000000000000000001", "01J00000000000000000000AB"),
        ],
        [
            {"vault_path": vault},
            {"vault": vault},
        ],
    )

    merged_text = (vault / "entities" / "people" / "example-person.md").read_text(encoding="utf-8")
    duplicate_text = duplicate.read_text(encoding="utf-8") if duplicate.exists() else ""
    reference_text = reference.read_text(encoding="utf-8")

    assert "Example Person Variant" in merged_text or "EP Variant" in merged_text
    assert "01J00000000000000000000002" in merged_text
    assert "[[example-person|Variant]]" in reference_text
    assert "[[example-person#Notes]]" in reference_text
    assert "[[example-person-duplicate" not in reference_text
    if duplicate.exists():
        assert "merged" in duplicate_text.lower() or "archived" in duplicate_text.lower()
    assert str(result)


def test_merged_tombstone_is_excluded_from_the_live_index(tmp_path: Path) -> None:
    # A merge archives the merged entity with a `merged_into` pointer. Because
    # entity_files() rglobs the whole entities/ tree (archive/ included), the
    # tombstone must be skipped at parse time or it resurfaces as a ghost node
    # that is still queryable and re-emits the merged entity's edges.
    vault = prepare_vault(tmp_path)
    duplicate = vault / "entities" / "people" / "example-person-duplicate.md"
    duplicate.write_text(
        """---
id: 01J00000000000000000000AB
type: person
name: Example Person Variant
aliases: [Example Person, EP Variant]
review_status: proposed
relations:
  - type: works_at
    target: 01J00000000000000000000002
    properties: {}
    source: test-fixture
---

# Example Person Variant

Variant notes.
""",
        encoding="utf-8",
    )

    merge_entities = call_first("merge_entities", "merge", "run_merge")
    invoke_with_variants(
        merge_entities,
        [("01J00000000000000000000001", "01J00000000000000000000AB")],
        [{"vault_path": vault}, {"vault": vault}],
    )

    from synapse.index import connect, reindex

    reindex(vault, full=True)
    conn = connect(vault)
    try:
        ids = {row["id"] for row in conn.execute("SELECT id FROM entities").fetchall()}
    finally:
        conn.close()

    assert "01J00000000000000000000AB" not in ids, "merged tombstone must not be indexed"
    assert "01J00000000000000000000001" in ids, "keep entity must survive"


def test_embed_rebuilds_vectors_and_supports_similarity_ranking(tmp_path: Path) -> None:
    embedder = getattr(maintenance, "embed_vault", None) or getattr(maintenance, "embed", None)
    if not callable(embedder):
        pytest.skip("synapse.maintenance does not yet expose embed_vault/embed")

    vault = prepare_vault(tmp_path)

    class FakeEmbedder:
        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [[1.0, 0.0], [0.9, 0.1], [0.1, 0.9]]

    fake_embedder = FakeEmbedder()
    result = invoke_with_variants(
        embedder,
        [()],
        [
            {"vault_path": vault, "embedder": fake_embedder, "all": True},
            {"vault": vault, "embedder": fake_embedder, "all": True},
            {"vault_path": vault, "embedder": fake_embedder, "all_": True},
            {"vault": vault, "embedder": fake_embedder, "all_": True},
        ],
    )
    assert fake_embedder.calls
    if result is not None:
        result_text = str(result)
        assert "embedding" in result_text.lower() or "vector" in result_text.lower()


def test_check_reports_oversized_relation_files(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    relations_specs = []
    for i in range(80):
        relations_specs.append({
            "type": "knows",
            "target": f"01J000000000000000000000{i:02d}",
            "properties": {},
            "source": "test"
        })
    bad_person = vault / "entities" / "people" / "bad-person.md"
    from synapse.util import write_frontmatter
    frontmatter = {
        "id": "01J00000000000000000000BAD",
        "type": "person",
        "name": "Oversized Person",
        "review_status": "proposed",
        "relations": relations_specs
    }
    write_frontmatter(bad_person, frontmatter, "# Oversized Person")
    
    check_vault = call_first("check_vault", "check", "run_check")
    report = invoke_with_variants(
        check_vault,
        [(), (vault,), (str(vault),)],
        [{"vault_path": vault}, {"vault": vault}],
    )
    oversized = report.get("oversized_relation_files", [])
    assert len(oversized) == 1
    assert oversized[0]["name"] == "Oversized Person"
    assert oversized[0]["count"] == 80


def test_merge_keep_side_wins_properties(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    # create keep entity
    keep_path = vault / "entities" / "people" / "keep-person.md"
    from synapse.util import read_frontmatter, write_frontmatter
    write_frontmatter(
        keep_path,
        {
            "id": "01J0000000000000000000000999",
            "type": "person",
            "name": "Keep Person",
            "review_status": "proposed",
            "properties": {
                "key1": "keep_val",
                "key2": "keep_val2",
                "linkedin_url": "https://linkedin.com/in/keep"
            }
        },
        "# Keep Person"
    )
    # create merge entity
    merge_path = vault / "entities" / "people" / "merge-person.md"
    write_frontmatter(
        merge_path,
        {
            "id": "01J0000000000000000000000998",
            "type": "person",
            "name": "Merge Person",
            "review_status": "proposed",
            "properties": {
                "key2": "merge_val2", # collision (keep wins)
                "key3": "merge_val3", # merge only (copied to keep)
                "linkedin_url": "https://linkedin.com/in/merge" # collision (urls alt)
            }
        },
        "Merge body content."
    )

    merge_entities = call_first("merge_entities", "merge", "run_merge")
    invoke_with_variants(
        merge_entities,
        [("01J0000000000000000000000999", "01J0000000000000000000000998")],
        [{"vault_path": vault}],
    )

    metadata, body = read_frontmatter(keep_path)
    props = metadata.get("properties") or {}
    assert props["key1"] == "keep_val"
    assert props["key2"] == "keep_val2"
    assert props["key3"] == "merge_val3"
    assert props["linkedin_url"] == "https://linkedin.com/in/keep"
    assert props["linkedin_urls_alt"] == ["https://linkedin.com/in/merge"]
    assert "Collisions:" in body
    assert "key2: merge_val2" in body
    assert "Merge body content." in body


def test_merge_retargets_direction_incoming_relations(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    from synapse.util import read_frontmatter, write_frontmatter
    keep_path = vault / "entities" / "people" / "keep-person.md"
    write_frontmatter(
        keep_path,
        {
            "id": "01J0000000000000000000000999",
            "type": "person",
            "name": "Keep Person",
            "review_status": "proposed",
            "relations": []
        },
        "# Keep Person"
    )
    merge_path = vault / "entities" / "people" / "merge-person.md"
    write_frontmatter(
        merge_path,
        {
            "id": "01J0000000000000000000000998",
            "type": "person",
            "name": "Merge Person",
            "review_status": "proposed",
            "relations": []
        },
        "# Merge Person"
    )
    # Create another entity with a direction: incoming relation pointing to merge-person
    other_path = vault / "entities" / "conversations" / "other-conv.md"
    write_frontmatter(
        other_path,
        {
            "id": "01J00000000000000000000003",
            "type": "conversation",
            "name": "Other Conv",
            "review_status": "proposed",
            "relations": [
                {
                    "type": "targets",
                    "target": "01J0000000000000000000000998",
                    "direction": "incoming"
                }
            ]
        },
        "# Other Conv"
    )

    merge_entities = call_first("merge_entities", "merge", "run_merge")
    invoke_with_variants(
        merge_entities,
        [("01J0000000000000000000000999", "01J0000000000000000000000998")],
        [{"vault_path": vault}],
    )

    other_meta, _ = read_frontmatter(other_path)
    rels = other_meta.get("relations") or []
    assert len(rels) == 1
    assert rels[0]["target"] == "01J0000000000000000000000999"
    assert rels[0]["direction"] == "incoming"

