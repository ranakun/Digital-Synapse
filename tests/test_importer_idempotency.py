"""T0.2 - Importer idempotency harness.

Running any importer twice against identical input must produce a
byte-identical vault (modulo .synapse/ internal state).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import synapse.importers as importers
from synapse.cli import app as cli_app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixture CSV paths - all under tests/fixtures/linkedin/
# ---------------------------------------------------------------------------
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "linkedin"

FIXTURE_CSVS: dict[str, Path] = {
    "import_linkedin_certifications": _FIXTURES_DIR / "certifications.csv",
    "import_linkedin_connections": _FIXTURES_DIR / "connections.csv",
    "import_linkedin_education": _FIXTURES_DIR / "education.csv",
    "import_linkedin_endorsements_given": _FIXTURES_DIR / "endorsements_given.csv",
    "import_linkedin_endorsements_received": _FIXTURES_DIR / "endorsements_received.csv",
    "import_linkedin_events": _FIXTURES_DIR / "events.csv",
    "import_linkedin_invitations": _FIXTURES_DIR / "invitations.csv",
    "import_linkedin_job_applications": _FIXTURES_DIR / "job_applications.csv",
    "import_linkedin_messages": _FIXTURES_DIR / "messages.csv",
    "import_linkedin_positions": _FIXTURES_DIR / "positions.csv",
    "import_linkedin_recommendations_given": _FIXTURES_DIR / "recommendations_given.csv",
    "import_linkedin_recommendations_received": _FIXTURES_DIR / "recommendations_received.csv",
    "import_linkedin_saved_jobs": _FIXTURES_DIR / "saved_jobs.csv",
    "import_linkedin_skills": _FIXTURES_DIR / "skills.csv",
}

# ---------------------------------------------------------------------------
# Dynamic completeness check - parametrize list must equal importers in module
# ---------------------------------------------------------------------------
_EXPECTED = sorted(
    name for name in dir(importers) if name.startswith("import_linkedin_")
)

# Verify that FIXTURE_CSVS covers exactly the set of discovered importers.
# This assertion fires at collection time so adding a new importer fails loudly.
assert sorted(FIXTURE_CSVS.keys()) == _EXPECTED, (
    f"FIXTURE_CSVS keys do not match discovered importers.\n"
    f"  Missing from FIXTURE_CSVS: {sorted(set(_EXPECTED) - set(FIXTURE_CSVS))}\n"
    f"  Extra in FIXTURE_CSVS:     {sorted(set(FIXTURE_CSVS) - set(_EXPECTED))}"
)

def _snapshot(vault: Path) -> dict[str, str]:
    """sha256 of every file under vault, excluding .synapse/ internal state."""
    result: dict[str, str] = {}
    for f in sorted(vault.rglob("*")):
        if f.is_file() and ".synapse" not in f.parts:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            result[str(f.relative_to(vault))] = digest
    return result


@pytest.mark.parametrize("fn_name", _EXPECTED)
def test_importer_idempotency(fn_name: str, tmp_path: Path) -> None:
    """Running an importer twice on identical input must not mutate the vault."""
    # --- 1. Initialise a fresh vault ----------------------------------------
    result = runner.invoke(cli_app, ["init", "--path", str(tmp_path), "--no-git"])
    assert result.exit_code == 0, (
        f"synapse init failed (exit {result.exit_code}):\n{result.output}"
    )

    # --- 2. Copy the fixture CSV into the vault inbox -----------------------
    fixture_path = FIXTURE_CSVS[fn_name]
    assert fixture_path.exists(), (
        f"Fixture CSV not found: {fixture_path}"
    )
    dest_csv = tmp_path / "fixture.csv"
    dest_csv.write_bytes(fixture_path.read_bytes())

    # --- 3. Call importer once ----------------------------------------------
    fn = getattr(importers, fn_name)
    fn(dest_csv, vault_path=tmp_path)

    snap1 = _snapshot(tmp_path)

    # --- 4. Call importer a second time with the same CSV -------------------
    fn(dest_csv, vault_path=tmp_path)

    snap2 = _snapshot(tmp_path)

    # --- 5. Assert byte-identical output ------------------------------------
    if snap1 != snap2:
        changed = {
            path: (snap1.get(path, "<absent>"), snap2.get(path, "<absent>"))
            for path in sorted(set(snap1) | set(snap2))
            if snap1.get(path) != snap2.get(path)
        }
        lines = [f"  {path}: {h1} -> {h2}" for path, (h1, h2) in changed.items()]
        raise AssertionError(
            f"Importer '{fn_name}' is NOT idempotent. "
            f"{len(changed)} file(s) changed on second run:\n" + "\n".join(lines)
        )
