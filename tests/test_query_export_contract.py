from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.cli import app


def command_names() -> set[str]:
    return {
        getattr(command, "name", None)
        for command in getattr(app, "registered_commands", [])
        if getattr(command, "name", None)
    }


REQUIRED_COMMANDS = {
    "find",
    "neighbors",
    "path",
    "filter",
    "query",
    "export",
    "serve",
    "import-linkedin-connections",
}
if not REQUIRED_COMMANDS.issubset(command_names()):
    pytest.skip("Phase 03 CLI commands are not implemented yet", allow_module_level=True)


FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


def run_cli(args: list[str]):
    return CliRunner().invoke(app, args)


def test_find_neighbors_path_and_filter_return_expected_nodes(tmp_path: Path) -> None:
    vault = FIXTURE_VAULT

    result = run_cli(["find", "Example P.", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Example Person" in result.output

    result = run_cli(["neighbors", "01J00000000000000000000001", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Example Person (01J00000000000000000000001) —works_at→ Example Company (01J00000000000000000000002)" in result.output
    assert "Example Person (01J00000000000000000000001) —leads→ Example Project Alpha (01J00000000000000000000003)" in result.output
    assert "Example Person (01J00000000000000000000001) —contributes_to→ Example Project Beta (01J00000000000000000000004)" in result.output

    result = run_cli(
        [
            "path",
            "01J00000000000000000000001",
            "01J00000000000000000000005",
            "--vault",
            str(vault),
        ]
    )
    assert result.exit_code == 0
    assert "Example Person (01J00000000000000000000001) —leads→ Example Project Alpha (01J00000000000000000000003)" in result.output

    result = run_cli(["filter", "--type", "person", "--tag", "crypto", "--vault", str(vault)])
    assert result.exit_code == 0
    filtered = json.loads(result.output)
    assert any(item["name"] == "Example Person" for item in filtered)
    assert all(item["type"] == "person" for item in filtered)

    # Test --no-reindex flags on all four commands
    result = run_cli(["find", "Example P.", "--vault", str(vault), "--no-reindex"])
    assert result.exit_code == 0
    assert "Example Person" in result.output

    result = run_cli(["neighbors", "01J00000000000000000000001", "--vault", str(vault), "--no-reindex"])
    assert result.exit_code == 0
    assert "—works_at→" in result.output

    result = run_cli(["path", "01J00000000000000000000001", "01J00000000000000000000005", "--vault", str(vault), "--no-reindex"])
    assert result.exit_code == 0
    assert "—leads→" in result.output

    result = run_cli(["filter", "--type", "person", "--tag", "crypto", "--vault", str(vault), "--no-reindex"])
    assert result.exit_code == 0
    filtered_nr = json.loads(result.output)
    assert any(item["name"] == "Example Person" for item in filtered_nr)


def test_query_disambiguation_reports_candidates(tmp_path: Path) -> None:
    import shutil

    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "*.db-journal"))
    (vault / "entities" / "companies" / "example-company-duplicate.md").write_text(
        """---
id: 01J00000000000000000000031
type: company
name: Example Company
aliases: [Example Co.]
review_status: verified
relations: []
---

# Example Company Duplicate
""",
        encoding="utf-8",
    )

    result = run_cli(["query", "how am I connected to Example Company?", "--vault", str(vault)])
    assert "Example Company" in result.output
    assert "disambigu" in result.output.lower() or "candidate" in result.output.lower()


def test_export_writes_self_contained_html(tmp_path: Path) -> None:
    output = tmp_path / "subgraph.html"

    result = run_cli(
        [
            "export",
            "neighbors Example Person",
            "--html",
            str(output),
            "--vault",
            str(FIXTURE_VAULT),
        ]
    )
    assert result.exit_code == 0
    assert output.exists()

    html = output.read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert "forcegraph" in html.lower() or "force-graph" in html.lower()
    assert "Example Person" in html
    assert "Example Project Alpha" in html
    # No external script/link tags that would require network access at render time.
    import re
    external_loads = re.findall(r'(?:src|href)\s*=\s*["\']https?://', html, re.IGNORECASE)
    assert not external_loads, f"Found external resource loads: {external_loads}"
