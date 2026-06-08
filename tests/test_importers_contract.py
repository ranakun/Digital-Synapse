from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.importers import import_linkedin_connections, parse_linkedin_connections_csv


def test_parse_linkedin_connections_tolerates_preamble_and_short_rows(tmp_path: Path) -> None:
    source = tmp_path / "Connections.csv"
    source.write_text(
        "Downloaded from LinkedIn\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Mira,Shah,https://www.linkedin.com/in/mira,ms@example.test,Acme Agents,Operator,2024-01-02\n"
        "NoCompany,Person,https://www.linkedin.com/in/nocompany\n",
        encoding="utf-8",
    )

    connections, warnings = parse_linkedin_connections_csv(source)

    assert len(connections) == 2
    assert connections[0].name == "Mira Shah"
    assert connections[0].company == "Acme Agents"
    assert connections[0].position == "Operator"
    assert connections[1].name == "NoCompany Person"
    assert any("fewer columns" in warning for warning in warnings)


def test_import_linkedin_connections_writes_proposed_entities_and_relations(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = CliRunner().invoke(app, ["init", "--path", str(vault)])
    assert result.exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(
            [
                "Mira",
                "Shah",
                "https://www.linkedin.com/in/mira",
                "ms@example.test",
                "Acme Agents",
                "Operator",
                "2024-01-02",
            ]
        )
        writer.writerow(
            [
                "Arun",
                "Rao",
                "https://www.linkedin.com/in/arun",
                "",
                "Acme Agents",
                "Engineer",
                "2023-11-10",
            ]
        )

    imported = import_linkedin_connections(source, vault=vault)

    assert imported["connections"] == 2
    assert imported["companies"] == 1
    assert (vault / "entities" / "people" / "mira-shah.md").exists()
    assert (vault / "entities" / "people" / "arun-rao.md").exists()
    assert (vault / "entities" / "companies" / "acme-agents.md").exists()

    person_text = (vault / "entities" / "people" / "mira-shah.md").read_text(encoding="utf-8")
    assert "review_status: proposed" in person_text
    assert "linkedin_url: https://www.linkedin.com/in/mira" in person_text
    assert "type: works_at" in person_text
    assert "role: Operator" in person_text

    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "entities/people/mira-shah.md" in status.replace("\\", "/")
