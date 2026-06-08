from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

queries = pytest.importorskip("synapse.queries")


def call_first(*candidate_names):
    for name in candidate_names:
        func = getattr(queries, name, None)
        if callable(func):
            return func
    pytest.fail("synapse.queries does not expose any of: " + ", ".join(candidate_names))


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


def test_query_plan_validation_accepts_known_operation() -> None:
    validate = call_first("validate_query_plan", "validate_plan", "check_query_plan")
    plan = {
        "operation": "neighbors",
        "params": {"start": "01J00000000000000000000001", "depth": 2, "include_weak": False},
        "entity_refs": [],
    }

    validated = validate(plan)
    assert read_value(validated, "operation") == "neighbors"
    params = read_value(validated, "params")
    assert read_value(params, "depth") == 2


def test_query_plan_validation_rejects_out_of_schema_operations() -> None:
    validate = call_first("validate_query_plan", "validate_plan", "check_query_plan")
    bad_plan = {
        "operation": "draft_email",
        "params": {"target": "Example Person"},
        "entity_refs": [],
    }

    with pytest.raises(Exception, match="operation|schema|draft_email"):
        validate(bad_plan)


def test_query_plan_validation_rejects_unknown_parameters_and_bad_types() -> None:
    validate = call_first("validate_query_plan", "validate_plan", "check_query_plan")

    with pytest.raises(Exception, match="parameter|draft"):
        validate(
            {
                "operation": "neighbors",
                "params": {"start": "Example Person", "draft": "summary"},
                "entity_refs": [],
            }
        )

    with pytest.raises(Exception, match="depth|positive"):
        validate(
            {
                "operation": "neighbors",
                "params": {"start": "Example Person", "depth": "2"},
                "entity_refs": [],
            }
        )

    with pytest.raises(Exception, match="entity_refs"):
        validate(
            {
                "operation": "find",
                "params": {"text": "Example Person"},
                "entity_refs": ["Example Person"],
            }
        )


def test_entity_reference_resolution_requires_disambiguation(tmp_path: Path) -> None:
    resolve = getattr(queries, "resolve_entity_refs", None)
    if not callable(resolve):
        pytest.skip("synapse.queries does not yet expose resolve_entity_refs")

    vault = tmp_path / "vault"
    (vault / "entities" / "companies").mkdir(parents=True)
    (vault / "entities" / "companies" / "example-company-a.md").write_text(
        """---
id: 01J00000000000000000000011
type: company
name: Example Company
aliases: [Example Co.]
review_status: verified
relations: []
---

# Example Company A
""",
        encoding="utf-8",
    )
    (vault / "entities" / "companies" / "example-company-b.md").write_text(
        """---
id: 01J00000000000000000000012
type: company
name: Example Company
aliases: [Example Co.]
review_status: verified
relations: []
---

# Example Company B
""",
        encoding="utf-8",
    )

    resolution = invoke_with_variants(
        resolve,
        [(["Example Company"],), ("Example Company",)],
        [{"vault_path": vault}, {"vault": vault}],
    )
    if isinstance(resolution, (list, tuple)):
        candidates = resolution
    else:
        candidates = read_value(resolution, "candidates", "matches", "entity_refs")
    assert len(candidates) == 2
