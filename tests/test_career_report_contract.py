from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from synapse.career import generate_career_report
from synapse.cli import app
from synapse.importers import import_linkedin_certifications, import_linkedin_positions

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


def test_career_report_includes_core_network_sections() -> None:
    report = generate_career_report(FIXTURE_VAULT, today=date(2026, 6, 9))

    assert "# Career Network Report" in report
    assert "Example Recruiter - Technical Recruiter at Talent Works" in report
    assert "Example Company: Example Person - CTO" in report
    assert "Example Senior Engineering Role" in report
    assert "skills: Example Skill" in report
    assert "Example Goal (goal)" in report
    assert "Example Person has stale last_contacted_on: 2025-01-01" in report
    assert "Example Career Meetup (event)" not in report


def test_career_report_cli_prints_or_writes_markdown(tmp_path: Path) -> None:
    runner = CliRunner()

    printed = runner.invoke(
        app,
        ["career-report", "--vault", str(FIXTURE_VAULT)],
    )
    assert printed.exit_code == 0
    assert "Likely Recruiters" in printed.output

    output = tmp_path / "career-report.md"
    written = runner.invoke(
        app,
        ["career-report", "--vault", str(FIXTURE_VAULT), "--output", str(output)],
    )
    assert written.exit_code == 0
    assert output.exists()
    assert "Career Network Report" in output.read_text(encoding="utf-8")


def test_career_report_includes_owner_certification_skill(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])
    assert result.exit_code == 0

    source = vault / "inbox" / "Certifications.csv"
    source.write_text(
        "Name,Url,Authority,Started On,Finished On,License Number\n"
        "Cryptography and Information Theory,https://example.test/cert,University of Colorado System,Dec 2024,,ABC123\n",
        encoding="utf-8",
    )
    import_linkedin_certifications(source, vault=vault)

    report = generate_career_report(vault, today=date(2026, 6, 9))

    assert "## Owner Skills And Certifications" in report
    assert "Cryptography and Information Theory" in report
    assert "authority: University of Colorado System" in report


def test_career_report_includes_owner_positions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])
    assert result.exit_code == 0

    source = vault / "inbox" / "Positions.csv"
    source.write_text(
        "Company Name,Title,Description,Location,Started On,Finished On\n"
        "Northstar Systems,Senior Research Engineer,MPC Cryptography,,Apr 2024,\n",
        encoding="utf-8",
    )
    import_linkedin_positions(source, vault=vault)

    report = generate_career_report(vault, today=date(2026, 6, 9))

    assert "## Owner Career History" in report
    assert "Senior Research Engineer at Northstar Systems" in report
    assert "2024-04 to present" in report
