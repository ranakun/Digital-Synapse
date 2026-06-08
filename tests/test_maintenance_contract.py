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

    shutil.copytree(fixture_vault, vault)
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

    assert "Example Person Variant" in merged_text or "EP Variant" in merged_text
    assert "01J00000000000000000000002" in merged_text
    if duplicate.exists():
        assert "merged" in duplicate_text.lower() or "archived" in duplicate_text.lower()
    assert str(result)


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
