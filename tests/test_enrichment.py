from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from synapse.cli import app
from synapse.enrichment import (
    _looks_like_noncompany,
    _should_replace_current_role,
    attach_linkedin_profile,
    generate_enrichment_queue,
    import_linkedin_profile_pdf,
    parse_linkedin_profile_text,
)
from synapse.importers import import_linkedin_connections
from synapse.index import reindex
from synapse.util import read_frontmatter, write_frontmatter


def _snapshot(vault: Path) -> dict[str, str]:
    return {
        str(path.relative_to(vault)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(vault.rglob("*"))
        if path.is_file() and ".synapse" not in path.parts
    }


def _make_profile_pdf(path: Path, *, company: str, role: str) -> None:
    fitz = pytest.importorskip("fitz")
    lines = [
        "Target Person",
        "Security and cryptography leader",
        "Harbor City, Testland",
        "Experience",
        company,
        role,
        "May 2026 - Present",
        "Harbor City, Testland",
        "Building custody infrastructure.",
        "Old Company",
        "Senior Engineer",
        "January 2020 - April 2026",
        "Harbor City, Testland",
        "Built distributed systems.",
        "Education",
        "Example Institute",
        "BTech Computer Science",
        "2012 - 2016",
        "Top Skills",
        "MPC Cryptography",
        "Golang",
        "Certifications",
        "Cloud Security",
    ]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 40
    for line in lines:
        page.insert_text((40, y), line, fontsize=10)
        y += 18
    doc.save(str(path))
    doc.close()


def test_profile_text_parser_extracts_supported_facts() -> None:
    text = """Target Person
Security and cryptography leader
Harbor City, Testland
Experience
New Company
Director of Security
May 2026 - Present
Harbor City, Testland
Building custody infrastructure.
Old Company
Senior Engineer
January 2020 - April 2026
Education
Example Institute
BTech Computer Science
2012 - 2016
Top Skills
MPC Cryptography
Golang
Certifications
Cloud Security
"""
    snapshot = parse_linkedin_profile_text(text, expected_name="Target Person")
    assert snapshot.headline == "Security and cryptography leader"
    assert snapshot.location == "Harbor City, Testland"
    assert len(snapshot.positions) == 2
    assert snapshot.positions[0].company == "New Company"
    assert snapshot.positions[0].finished_on == ""
    assert snapshot.positions[1].finished_on == "2026-04"
    assert snapshot.education[0].school == "Example Institute"
    assert snapshot.skills == ("MPC Cryptography", "Golang")
    assert snapshot.certifications == ("Cloud Security",)


def test_profile_parser_rejects_duration_location_and_fragment_headers() -> None:
    # Reproduces the E1 failure mode: LinkedIn interleaves duration lines, a bare
    # location, and a wrapped description tail before the next date range. The old
    # heuristic minted "2 years 3 months", "Singapore", and the sentence tail as
    # employers. The parser must keep only the real company/title headers.
    text = """Target Person
Security lead
Singapore
Experience
Real Company
Staff Engineer
January 2023 - Present
2 years 3 months
Singapore
Led custody work
and product development.
Prior Company
Senior Engineer
March 2019 - December 2022
3 years 10 months
Harbor City, Testland
Built distributed systems
Education
Example Institute
BTech
2012 - 2016
"""
    snapshot = parse_linkedin_profile_text(text, expected_name="Target Person")
    companies = {position.company for position in snapshot.positions}
    assert companies == {"Real Company", "Prior Company"}
    # None of the junk classes may surface as an employer.
    for junk in ("2 years 3 months", "3 years 10 months", "Singapore", "and product development."):
        assert junk not in companies
    assert {position.title for position in snapshot.positions} == {
        "Staff Engineer",
        "Senior Engineer",
    }


@pytest.mark.parametrize(
    "junk",
    [
        # PDF pagination footer that leaks into the text stream.
        "Page 2 of 3",
        "Page 1 of 4",
        # Bare locations LinkedIn lists on their own line (no comma).
        "Israel",
        "Hong Kong",
        "Moscow Domodedovo Airport",
        # Uppercase-start narrative lines truncated mid-sentence (no period),
        # pulled into the header window from an achievement bullet.
        "Left Aerohive to join Facebook",
        "Improved recruiter productivity and hiring outcomes through structured",
        "Laid the foundation for progression into lead and managerial responsibilities",
        "Main focus on tech roles within C/C++/embedded software development / Java",
    ],
)
def test_noncompany_filter_rejects_pagination_locations_and_narrative(junk: str) -> None:
    # Regression for the E1 reimport: the fixed parser still minted these three
    # mechanically-detectable junk classes as employers. They must be rejected.
    assert _looks_like_noncompany(junk) is True


@pytest.mark.parametrize(
    "real",
    [
        "Securosys SA",
        "Managed Services",  # narrative starter but < 5 words -> kept
        "Various Organizations",
        "Success Pact Consulting Pvt. Ltd.",
        "Bank of America",
        "Center for Internet Security",
        "ZebPay",
        "Ethereum Foundation",
        # Real employers a bare-comma / country-word rule must NOT drop.
        "Cornami, Inc.",
        "Honda Research Institute USA, Inc.",
        "Gavi, the Vaccine Alliance",
        "WalletConnect, Inc.",
        "Example Research Institute",
        "KPMG India",
        "Delhi Public School - India",
        "Indian Institute of Technology, Bombay",
        # Modern lowercase-first brand names.
        "fija Finance",
        "inspire AG",
        "myHQ by ANAROCK",
        "xNerds Solutions",
        "x-Biz Techventures Private Limited",
    ],
)
def test_noncompany_filter_keeps_real_employers(real: str) -> None:
    # Guard against over-broad heuristics dropping genuine (sometimes oddly
    # named or long) employers.
    assert _looks_like_noncompany(real) is False


def test_connections_evidence_wins_equal_date_profile_conflict() -> None:
    replace, warning = _should_replace_current_role(
        {
            "current_company": "Archive Company",
            "current_role": "Archive Role",
            "current_role_evidence": {
                "source": "linkedin-connections",
                "observed_at": "2026-06-15",
            },
        },
        company="Profile Company",
        role="Profile Role",
        captured_at="2026-06-15",
    )
    assert replace is False
    assert warning and "source precedence" in warning


def test_queue_is_stable_and_attachment_updates_only_target(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault), "--no-git"]).exit_code == 0
    write_frontmatter(
        vault / "entities" / "people" / "recruiter.md",
        {
            "id": "01J000000000000000000000A1",
            "type": "person",
            "name": "Recruiter One",
            "aliases": [],
            "review_status": "proposed",
            "tags": ["linkedin", "recruiter"],
            "relations": [],
            "properties": {
                "linkedin_url": "https://linkedin.com/in/recruiter-one",
                "current_role": "Talent Partner",
                "current_company": "Custody Labs",
            },
        },
        "# Recruiter One",
    )
    write_frontmatter(
        vault / "entities" / "people" / "engineer.md",
        {
            "id": "01J000000000000000000000A2",
            "type": "person",
            "name": "Engineer Two",
            "aliases": [],
            "review_status": "proposed",
            "tags": ["linkedin"],
            "relations": [],
            "properties": {
                "linkedin_url": "https://linkedin.com/in/engineer-two",
                "current_role": "Engineer",
                "current_company": "General Company",
            },
        },
        "# Engineer Two",
    )
    write_frontmatter(
        vault / "entities" / "conversations" / "recruiter-chat.md",
        {
            "id": "01J000000000000000000000C1",
            "type": "conversation",
            "name": "Recruiter chat",
            "aliases": [],
            "review_status": "proposed",
            "tags": ["conversation"],
            "relations": [
                {
                    "type": "participated_in",
                    "target": "01J000000000000000000000A1",
                    "direction": "incoming",
                }
            ],
            "properties": {
                "message_count": 12,
                "last_message_at": "2026-06-01T09:30:00",
            },
        },
        "# Recruiter chat",
    )
    reindex(vault, full=True)

    first = generate_enrichment_queue(vault, limit=2, today=None)
    queue_path = Path(first["queue"])
    before = queue_path.read_bytes()
    second = generate_enrichment_queue(vault, limit=2, today=None)
    assert queue_path.read_bytes() == before
    assert second["ranked"][0]["person_id"] == "01J000000000000000000000A1"
    assert second["ranked"][0]["score"] > second["ranked"][1]["score"]

    pdf = tmp_path / "profile.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic attachment")
    result = attach_linkedin_profile(
        pdf,
        person_id="01J000000000000000000000A1",
        captured_at="2026-06-15",
        vault=vault,
    )
    data = yaml.safe_load(queue_path.read_text(encoding="utf-8"))
    items = {item["person_id"]: item for item in data["items"]}
    assert items["01J000000000000000000000A1"]["state"] == "captured"
    assert items["01J000000000000000000000A2"]["state"] == "pending"
    assert Path(result["source"]).exists()


def test_profile_import_precedence_and_idempotency(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(
        app,
        ["init", "--path", str(vault), "--owner-name", "Owner", "--no-git"],
    ).exit_code == 0
    person_path = vault / "entities" / "people" / "target-person.md"
    write_frontmatter(
        person_path,
        {
            "id": "01J000000000000000000000P1",
            "type": "person",
            "name": "Target Person",
            "aliases": [],
            "review_status": "verified",
            "tags": ["linkedin"],
            "relations": [],
            "properties": {
                "linkedin_url": "https://linkedin.com/in/target",
                "current_company": "Archive Company",
                "current_role": "Archive Role",
                "current_role_evidence": {
                    "source": "linkedin-connections",
                    "observed_at": "2026-06-10",
                    "source_file": "Connections.csv",
                },
            },
        },
        "# Target Person",
    )
    reindex(vault, full=True)

    older = tmp_path / "older.pdf"
    _make_profile_pdf(older, company="Older Snapshot Company", role="Older Snapshot Role")
    old_result = import_linkedin_profile_pdf(
        older,
        person_id="01J000000000000000000000P1",
        captured_at="2026-05-01",
        vault=vault,
    )
    metadata, body = read_frontmatter(person_path)
    assert metadata["properties"]["current_company"] == "Archive Company"
    assert metadata["properties"]["current_role"] == "Archive Role"
    assert any("without overwriting" in warning for warning in old_result["warnings"])
    assert "Older Snapshot Company" in body
    assert any(
        relation["type"] == "former_employee_of"
        for relation in metadata["relations"]
    )

    newer = tmp_path / "newer.pdf"
    _make_profile_pdf(newer, company="New Snapshot Company", role="New Snapshot Role")
    import_linkedin_profile_pdf(
        newer,
        person_id="01J000000000000000000000P1",
        captured_at="2026-06-15",
        vault=vault,
    )
    metadata, _ = read_frontmatter(person_path)
    assert metadata["properties"]["current_company"] == "New Snapshot Company"
    assert metadata["properties"]["current_role"] == "New Snapshot Role"
    assert metadata["properties"]["current_role_evidence"]["observed_at"] == "2026-06-15"
    assert metadata["review_status"] == "proposed"

    before = _snapshot(vault)
    import_linkedin_profile_pdf(
        newer,
        person_id="01J000000000000000000000P1",
        captured_at="2026-06-15",
        vault=vault,
    )
    assert _snapshot(vault) == before


def test_profile_import_replaces_untimestamped_connections_employment(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(
        app,
        ["init", "--path", str(vault), "--owner-name", "Owner", "--no-git"],
    ).exit_code == 0
    person_path = vault / "entities" / "people" / "target-person.md"
    write_frontmatter(
        person_path,
        {
            "id": "01J000000000000000000000P1",
            "type": "person",
            "name": "Target Person",
            "aliases": [],
            "review_status": "verified",
            "tags": ["linkedin", "recruiter"],
            "relations": [
                {
                    "type": "works_at",
                    "target": "01J000000000000000000000C1",
                    "source": "Connections.csv",
                },
                {
                    "type": "recruits_for",
                    "target": "01J000000000000000000000C1",
                    "source": "Connections.csv",
                },
            ],
            "properties": {
                "current_company": "Old Company",
                "current_role": "Senior Engineer",
            },
        },
        "# Target Person",
    )
    write_frontmatter(
        vault / "entities" / "companies" / "old-company.md",
        {
            "id": "01J000000000000000000000C1",
            "type": "company",
            "name": "Old Company",
            "aliases": [],
            "review_status": "proposed",
            "tags": [],
            "relations": [],
            "properties": {},
        },
        "# Old Company",
    )
    reindex(vault, full=True)

    profile = tmp_path / "profile.pdf"
    _make_profile_pdf(profile, company="New Company", role="New Role")
    import_linkedin_profile_pdf(
        profile,
        person_id="01J000000000000000000000P1",
        captured_at="2026-06-15",
        vault=vault,
    )

    metadata, _ = read_frontmatter(person_path)
    assert metadata["properties"]["current_company"] == "New Company"
    assert metadata["properties"]["current_role"] == "New Role"
    assert not any(
        relation.get("target") == "01J000000000000000000000C1"
        and relation.get("type") in ("works_at", "recruits_for")
        for relation in metadata["relations"]
    )
    assert any(
        relation.get("target") == "01J000000000000000000000C1"
        and relation.get("type") == "former_employee_of"
        for relation in metadata["relations"]
    )


def test_profile_import_requires_explicit_existing_person(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault), "--no-git"]).exit_code == 0
    pdf = tmp_path / "profile.pdf"
    _make_profile_pdf(pdf, company="Company", role="Role")
    with pytest.raises(ValueError, match="No person entity"):
        import_linkedin_profile_pdf(
            pdf,
            person_id="Target Person",
            captured_at="2026-06-15",
            vault=vault,
        )


def test_connections_import_records_source_observation_date(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault), "--no-git"]).exit_code == 0
    source = tmp_path / "Connections.csv"
    source.write_text(
        "First Name,Last Name,URL,Company,Position,Connected On\n"
        "Alice,Smith,https://linkedin.com/in/alice,Acme,Security Engineer,2024-01-15\n",
        encoding="utf-8",
    )
    observed = datetime(2026, 6, 10, 12, 0, tzinfo=UTC).timestamp()
    os.utime(source, (observed, observed))

    import_linkedin_connections(source, vault=vault)

    metadata, _ = read_frontmatter(vault / "entities" / "people" / "alice-smith.md")
    assert metadata["properties"]["current_role_evidence"] == {
        "source": "linkedin-connections",
        "observed_at": "2026-06-10",
        "source_file": "Connections.csv",
    }
    works_at = next(
        relation for relation in metadata["relations"] if relation["type"] == "works_at"
    )
    assert works_at["properties"]["observed_at"] == "2026-06-10"
