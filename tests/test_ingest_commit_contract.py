from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ingest = pytest.importorskip("synapse.ingest")


def call_first(*candidate_names):
    for name in candidate_names:
        func = getattr(ingest, name, None)
        if callable(func):
            return func
    pytest.fail("synapse.ingest does not expose the expected entry points")


def read_value(obj, *names):
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AssertionError(f"missing expected field(s): {names}")


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


class FakeGenerator:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.response


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


def prepare_vault(tmp_path: Path) -> Path:
    fixture_vault = Path(__file__).parent / "fixtures" / "vault"
    vault = tmp_path / "vault"
    import shutil

    shutil.copytree(fixture_vault, vault)
    git_init(vault)
    return vault


def test_ingest_writes_proposed_changes_and_keeps_source_in_inbox(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    source = vault / "inbox" / "sample-ingest.md"
    source.write_text(
        """Example Person is now the owner of Example Project Beta.
This is a safe fake source document for test coverage.
""",
        encoding="utf-8",
    )

    fake_generator = FakeGenerator(
        {
            "changeset": [
                {
                    "op": "update",
                    "type": "person",
                    "matched_existing": "01J00000000000000000000001",
                    "name": "Example Person",
                    "confidence": 0.91,
                    "properties": {"location": "Singapore"},
                    "relations": [
                        {"type": "works_at", "target": "Example Company", "properties": {}}
                    ],
                    "body_append": "Ingested from sample source.",
                }
            ],
            "ambiguities": [],
        }
    )

    ingest_file = call_first("ingest_file", "ingest", "run_ingest")
    result = invoke_with_variants(
        ingest_file,
        [(source,), (str(source),)],
        [
            {"vault_path": vault, "generator": fake_generator},
            {"vault": vault, "generator": fake_generator},
            {"vault_path": vault, "provider": fake_generator},
            {"vault": vault, "provider": fake_generator},
        ],
    )

    assert fake_generator.requests, "the fake generator should be invoked"
    assert source.exists(), "ingest must leave the raw source in inbox until commit"
    person_file = vault / "entities" / "people" / "example-person.md"
    contents = person_file.read_text(encoding="utf-8")
    assert "review_status: proposed" in contents
    assert "Ingested from sample source." in contents
    if result is not None:
        assert str(result)


def test_candidate_context_prioritizes_entities_mentioned_in_source(tmp_path: Path) -> None:
    from synapse.index import reindex

    vault = tmp_path / "vault"
    (vault / "entities" / "companies").mkdir(parents=True)

    for index in range(60):
        name = f"Alpha Company {index:02d}"
        entity_id = f"01J0000000000000000001{index:03d}"
        (vault / "entities" / "companies" / f"alpha-company-{index:02d}.md").write_text(
            f"""---
id: {entity_id}
type: company
name: {name}
aliases: []
review_status: verified
relations: []
---

# {name}
""",
            encoding="utf-8",
        )

    (vault / "entities" / "companies" / "zebra-robotics.md").write_text(
        """---
id: 01J0000000000000000001999
type: company
name: Zebra Robotics
aliases: [Zebra]
review_status: verified
relations: []
---

# Zebra Robotics
""",
        encoding="utf-8",
    )
    reindex(vault, full=True)

    candidates = ingest.candidate_context(
        vault,
        "Met with Zebra Robotics about operator tooling and a possible pilot.",
        limit=5,
    )

    assert candidates[0]["name"] == "Zebra Robotics"
    assert len(candidates) <= 5


def test_ingest_provider_preflight_failure_leaves_vault_unchanged(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    source = vault / "inbox" / "sample-ingest.md"
    source.write_text("Example Person at Example Company.", encoding="utf-8")

    class RefusingGenerator:
        def complete(self, request):
            raise RuntimeError("provider unavailable")

    ingest_file = call_first("ingest_file", "ingest", "run_ingest")
    before = (vault / "entities" / "people" / "example-person.md").read_text(encoding="utf-8")

    with pytest.raises(Exception):
        invoke_with_variants(
            ingest_file,
            [(source,), (str(source),)],
            [
                {"vault_path": vault, "generator": RefusingGenerator()},
                {"vault": vault, "generator": RefusingGenerator()},
                {"vault_path": vault, "provider": RefusingGenerator()},
                {"vault": vault, "provider": RefusingGenerator()},
            ],
        )

    after = (vault / "entities" / "people" / "example-person.md").read_text(encoding="utf-8")
    assert after == before
    assert source.exists()


def test_commit_flips_statuses_archives_sources_and_creates_git_commit(tmp_path: Path) -> None:
    vault = prepare_vault(tmp_path)
    source = vault / "inbox" / "sample-ingest.md"
    source.write_text("Example Person now owns Example Project Beta.", encoding="utf-8")

    fake_generator = FakeGenerator(
        {
            "changeset": [
                {
                    "op": "update",
                    "type": "person",
                    "matched_existing": "01J00000000000000000000001",
                    "name": "Example Person",
                    "confidence": 0.95,
                    "properties": {},
                    "relations": [],
                    "body_append": "Pending review.",
                }
            ],
            "ambiguities": [],
        }
    )

    ingest_file = call_first("ingest_file", "ingest", "run_ingest")
    commit_vault = call_first("commit_vault", "commit", "finalize_commit")

    invoke_with_variants(
        ingest_file,
        [(source,), (str(source),)],
        [
            {"vault_path": vault, "generator": fake_generator},
            {"vault": vault, "generator": fake_generator},
            {"vault_path": vault, "provider": fake_generator},
            {"vault": vault, "provider": fake_generator},
        ],
    )

    import subprocess

    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault, check=True, capture_output=True, text=True
    ).stdout.strip()

    commit_result = invoke_with_variants(
        commit_vault,
        [(), (vault,), (str(vault),)],
        [
            {"vault_path": vault, "message": "Test commit"},
            {"vault": vault, "message": "Test commit"},
            {"vault_path": vault, "msg": "Test commit"},
            {"vault": vault, "msg": "Test commit"},
        ],
    )

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert before != after
    assert not source.exists()
    assert (vault / "inbox" / "processed" / "sample-ingest.md").exists()
    assert "review_status: verified" in (
        vault / "entities" / "people" / "example-person.md"
    ).read_text(encoding="utf-8")
    if commit_result is not None:
        assert str(commit_result)
