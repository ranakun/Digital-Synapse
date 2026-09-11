from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.index import reindex
from synapse.queries import find_entities

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

@pytest.fixture(scope="module", autouse=True)
def init_vault():
    reindex(FIXTURE_VAULT, full=False)


def test_fts_hardening_hostile_inputs() -> None:
    # Hostile inputs must not raise exceptions (especially sqlite3.OperationalError).
    # Since we test that they return without error, we also check if they can match or fallback.
    
    # 1. "c++" - should clean to "c" and search or fallback.
    res = find_entities(FIXTURE_VAULT, "c++", reindex=False)
    assert isinstance(res, list)

    # 2. '"quoted"' - double quotes stripped/handled.
    res = find_entities(FIXTURE_VAULT, '"quoted"', reindex=False)
    assert isinstance(res, list)

    # 3. "mpc/hsm" - slash stripped/handled.
    res = find_entities(FIXTURE_VAULT, "mpc/hsm", reindex=False)
    assert isinstance(res, list)

    # 4. "a-b" - hyphen stripped/handled.
    res = find_entities(FIXTURE_VAULT, "a-b", reindex=False)
    assert isinstance(res, list)

    # 5. "term*" - asterisk stripped/handled.
    res = find_entities(FIXTURE_VAULT, "term*", reindex=False)
    assert isinstance(res, list)

    # 6. emoji - emoji should pass through or not crash.
    res = find_entities(FIXTURE_VAULT, "🚀", reindex=False)
    assert isinstance(res, list)

    # 7. empty string - should not crash, should return empty or all.
    res = find_entities(FIXTURE_VAULT, "", reindex=False)
    assert isinstance(res, list)


def test_fts_hardening_fallback_tracker() -> None:
    # Verify fallback_tracker records when substring fallback is hit.
    # An empty query or a query with no matches should trigger substring fallback.
    tracker = []
    _ = find_entities(FIXTURE_VAULT, "nonexistent_term_that_will_not_be_found", reindex=False, fallback_tracker=tracker)
    assert "substring" in tracker
