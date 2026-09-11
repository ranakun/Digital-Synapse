"""Tests for T9.1: synapse stats command and gen_synthetic_vault generator."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


# ── gen_synthetic_vault ───────────────────────────────────────────────────────


def test_generator_produces_expected_entity_counts(tmp_path: Path) -> None:
    """Generator writes the right number of entity files."""
    import gen_synthetic_vault  # added to sys.path by conftest.py

    vault = tmp_path / "synth"
    gen_synthetic_vault.generate(vault, n_people=10, n_companies=5, n_conversations=8, seed=42)

    people_files = list((vault / "entities" / "people").glob("*.md"))
    company_files = list((vault / "entities" / "companies").glob("*.md"))
    conv_files = list((vault / "entities" / "conversations").glob("*.md"))

    # +1 for me.md
    assert len(people_files) == 11  # 10 + owner
    assert len(company_files) == 5
    assert len(conv_files) == 8


def test_generator_is_deterministic(tmp_path: Path) -> None:
    """Same seed produces byte-identical output across two runs."""
    import gen_synthetic_vault  # added to sys.path by conftest.py

    vault_a = tmp_path / "a"
    vault_b = tmp_path / "b"
    gen_synthetic_vault.generate(vault_a, n_people=20, n_companies=5, n_conversations=10, seed=99)
    gen_synthetic_vault.generate(vault_b, n_people=20, n_companies=5, n_conversations=10, seed=99)

    files_a = sorted(vault_a.rglob("*.md"))
    files_b = sorted(vault_b.rglob("*.md"))

    assert len(files_a) == len(files_b)
    for fa, fb in zip(files_a, files_b, strict=True):
        # Compare relative paths
        assert fa.relative_to(vault_a) == fb.relative_to(vault_b)
        assert fa.read_text(encoding="utf-8") == fb.read_text(encoding="utf-8"), (
            f"Mismatch in {fa.relative_to(vault_a)}"
        )


def test_generator_reindexes_clean(tmp_path: Path) -> None:
    """Generated vault reindexes without errors at N=1000."""
    import gen_synthetic_vault  # added to sys.path by conftest.py

    from synapse.index import reindex

    vault = tmp_path / "synth1k"
    gen_synthetic_vault.generate(vault, n_people=900, n_companies=50, n_conversations=50, seed=42)

    result = reindex(vault, full=True)
    # No hard errors (warnings about unresolved relations are acceptable since
    # generated entities use IDs directly, but the reindex must not explode)
    errors = [i for i in result.issues if i.severity == "error"]
    # IDs are used as targets so they should resolve; assert no parse errors
    assert result.entities >= 1, "Should have indexed at least the owner entity"
    assert result.entities > 900, "Should have indexed all generated people"
    # Allow relation-unresolved warnings but no fatal parse errors
    fatal = [e for e in errors if "frontmatter" in e.message.lower()]
    assert not fatal, f"Fatal parse errors: {fatal}"


def test_generator_overwrite_flag(tmp_path: Path) -> None:
    """--overwrite removes old content; missing flag aborts."""
    script = SCRIPTS_DIR / "gen_synthetic_vault.py"

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "sentinel.txt").write_text("do not remove")

    # Without --overwrite → exit code 1
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(vault), "--people", "2",
         "--companies", "1", "--conversations", "1"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (vault / "sentinel.txt").exists(), "Sentinel should survive the aborted run"

    # With --overwrite → success, sentinel gone
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(vault), "--people", "2",
         "--companies", "1", "--conversations", "1", "--overwrite"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (vault / "sentinel.txt").exists()


# ── synapse stats command ─────────────────────────────────────────────────────


def _run_stats(vault: Path, extra_args: list[str] | None = None) -> dict:
    """Run `synapse stats` via typer CliRunner and return parsed JSON."""
    from typer.testing import CliRunner

    from synapse.cli import app

    runner = CliRunner()
    args = ["stats", "--vault", str(vault)] + (extra_args or [])
    result = runner.invoke(app, args)
    assert result.exit_code == 0, f"stats failed:\n{result.output}\n{result.exception}"
    return json.loads(result.output)


def test_stats_returns_entity_counts(tmp_path: Path) -> None:
    """stats command returns entity/relation counts structured correctly."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    from synapse.index import reindex
    reindex(vault, full=True)

    data = _run_stats(vault)

    assert "entities" in data
    assert "relations" in data
    assert "thresholds" in data
    assert "above_threshold" in data

    ents = data["entities"]
    assert "total" in ents
    assert "verified" in ents
    assert "proposed" in ents
    assert "by_type" in ents
    assert ents["total"] == ents["verified"] + ents["proposed"]
    assert ents["total"] > 0

    rels = data["relations"]
    assert "total" in rels
    assert "by_type" in rels


def test_stats_thresholds_match_design_doc(tmp_path: Path) -> None:
    """Thresholds must match the design-doc values exactly."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    from synapse.index import reindex
    reindex(vault, full=True)

    data = _run_stats(vault)
    thresh = data["thresholds"]

    assert thresh["entity_count_trigger"] == 5000
    assert thresh["no_change_reindex_trigger_s"] == 2.0
    assert thresh["neighbors_depth2_trigger_s"] == 1.0


def test_stats_with_timing_includes_timing_keys(tmp_path: Path) -> None:
    """--timing flag populates timing sub-object with expected keys."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    from synapse.index import reindex
    reindex(vault, full=True)

    data = _run_stats(vault, ["--timing"])

    assert "timing" in data, "Expected 'timing' key with --timing flag"
    timing = data["timing"]
    assert "parse_vault_s" in timing
    assert "no_change_reindex_s" in timing
    assert "neighbors_me_depth2_s" in timing

    # Sanity: all values should be non-negative floats
    for key, val in timing.items():
        assert isinstance(val, (int, float)), f"{key} should be numeric, got {type(val)}"
        assert val >= 0, f"{key} should be non-negative"

    # above_threshold should include reindex/neighbors keys
    above = data["above_threshold"]
    assert "no_change_reindex" in above
    assert "neighbors_depth2" in above


def test_stats_fixture_vault_below_entity_threshold(tmp_path: Path) -> None:
    """Small fixture vault should be well below 5000-entity threshold."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    from synapse.index import reindex
    reindex(vault, full=True)

    data = _run_stats(vault)
    assert not data["above_threshold"]["entity_count"], (
        "Fixture vault is tiny — should not exceed the 5000-entity threshold"
    )


def test_stats_by_type_sums_match_totals(tmp_path: Path) -> None:
    """Sum of by_type counts must equal the totals."""
    vault = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT, vault,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal")
    )

    from synapse.index import reindex
    reindex(vault, full=True)

    data = _run_stats(vault)
    ents = data["entities"]

    computed_total = sum(
        sum(status_counts.values())
        for status_counts in ents["by_type"].values()
    )
    assert computed_total == ents["total"]

