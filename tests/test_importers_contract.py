from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.importers import (
    import_linkedin_certifications,
    import_linkedin_connections,
    import_linkedin_positions,
    is_recruiter_title,
    parse_linkedin_certifications_csv,
    parse_linkedin_connections_csv,
    parse_linkedin_positions_csv,
)
from synapse.util import read_frontmatter


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


def test_parse_linkedin_connections_normalizes_connected_on_dates(tmp_path: Path) -> None:
    source = tmp_path / "Connections.csv"
    source.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Mira,Shah,https://www.linkedin.com/in/mira,,Acme Agents,Operator,25 May 2026\n"
        "Arun,Rao,https://www.linkedin.com/in/arun,,Acme Agents,Engineer,2023-11-10\n"
        "Tara,Talent,https://www.linkedin.com/in/tara,,Talent Works,Partner,not a date\n",
        encoding="utf-8",
    )

    connections, _ = parse_linkedin_connections_csv(source)

    by_name = {connection.name: connection for connection in connections}
    # LinkedIn "DD Mon YYYY" form is normalized to ISO.
    assert by_name["Mira Shah"].connected_on == "2026-05-25"
    # Values already in ISO form are left unchanged.
    assert by_name["Arun Rao"].connected_on == "2023-11-10"
    # Unparseable values fall back to the raw string rather than failing.
    assert by_name["Tara Talent"].connected_on == "not a date"


def test_parse_linkedin_certifications_normalizes_month_year_and_dedupes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Certifications.csv"
    source.write_text(
        "Name,Url,Authority,Started On,Finished On,License Number\n"
        "Cryptography and Information Theory,https://example.test/cert,University of Colorado System,Dec 2024,,ABC123\n"
        "Cryptography and Information Theory,https://example.test/cert,University of Colorado System,Dec 2024,,ABC123\n"
        "No Date Cert,,Issuer,not a date,Jan 2026,\n",
        encoding="utf-8",
    )

    certifications, warnings = parse_linkedin_certifications_csv(source)

    assert len(certifications) == 2
    assert certifications[0].started_on == "2024-12"
    assert certifications[0].finished_on == ""
    assert certifications[1].started_on == "not a date"
    assert certifications[1].finished_on == "2026-01"
    assert any("duplicate" in warning for warning in warnings)


def test_parse_linkedin_positions_normalizes_dates_and_cleans_text(tmp_path: Path) -> None:
    source = tmp_path / "Positions.csv"
    broken_bullet = "\u00e2\u20ac\u00a2"
    source.write_text(
        "Company Name,Title,Description,Location,Started On,Finished On\n"
        f'Acme,Engineer,"{broken_bullet} Built APIs {broken_bullet} Shipped systems",Remote,Apr 2024,\n'
        f'Acme,Engineer,"{broken_bullet} Built APIs {broken_bullet} Shipped systems",Remote,Apr 2024,\n'
        "Beta,Intern,Documented work,,Jul 2022,Dec 2022\n",
        encoding="utf-8",
    )

    positions, warnings = parse_linkedin_positions_csv(source)

    assert len(positions) == 2
    assert positions[0].started_on == "2024-04"
    assert positions[0].finished_on == ""
    assert positions[0].description == "\u2022 Built APIs \u2022 Shipped systems"
    assert positions[1].started_on == "2022-07"
    assert positions[1].finished_on == "2022-12"
    assert any("duplicate" in warning for warning in warnings)


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
        writer.writerow(
            [
                "Tara",
                "Talent",
                "https://www.linkedin.com/in/tara",
                "",
                "Talent Works",
                "Talent Acquisition Partner",
                "2023-09-01",
            ]
        )

    imported = import_linkedin_connections(source, vault=vault)

    assert imported["connections"] == 3
    assert imported["companies"] == 2
    assert (vault / "entities" / "people" / "mira-shah.md").exists()
    assert (vault / "entities" / "people" / "arun-rao.md").exists()
    assert (vault / "entities" / "people" / "tara-talent.md").exists()
    assert (vault / "entities" / "companies" / "acme-agents.md").exists()

    person_text = (vault / "entities" / "people" / "mira-shah.md").read_text(encoding="utf-8")
    assert "review_status: proposed" in person_text
    assert "linkedin_url: https://www.linkedin.com/in/mira" in person_text
    assert "current_company: Acme Agents" in person_text
    assert "current_role: Operator" in person_text
    assert "type: works_at" in person_text
    assert "role: Operator" in person_text

    recruiter_text = (vault / "entities" / "people" / "tara-talent.md").read_text(
        encoding="utf-8"
    )
    assert "- recruiter" in recruiter_text
    assert "current_role: Talent Acquisition Partner" in recruiter_text

    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "entities/people/mira-shah.md" in status.replace("\\", "/")


def test_import_linkedin_certifications_creates_skill_and_owner_relation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    assert (
        CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"]).exit_code
        == 0
    )

    source = vault / "inbox" / "Certifications.csv"
    source.write_text(
        "Name,Url,Authority,Started On,Finished On,License Number\n"
        "Cryptography and Information Theory,https://example.test/cert,University of Colorado System,Dec 2024,,ABC123\n",
        encoding="utf-8",
    )

    imported = import_linkedin_certifications(source, vault=vault)
    imported_again = import_linkedin_certifications(source, vault=vault)

    assert imported["certifications"] == 1
    assert imported["demonstrates_skill_edges"] == 1
    assert len(imported["created"]) == 1
    assert len(imported_again["updated"]) == 1

    skill = vault / "entities" / "skills" / "cryptography-and-information-theory.md"
    skill_text = skill.read_text(encoding="utf-8")
    assert "type: skill" in skill_text
    assert "- certification" in skill_text
    assert "credential_authority: University of Colorado System" in skill_text
    assert "credential_started_on: 2024-12" in skill_text
    assert "LinkedIn certification associated with [[me|Owner]]." in skill_text
    assert skill_text.count("LinkedIn certification associated with [[me|Owner]].") == 1

    me = (vault / "entities" / "people" / "me.md").read_text(encoding="utf-8")
    assert "type: demonstrates_skill" in me
    assert "credential_license_number: ABC123" in me
    assert me.count("type: demonstrates_skill") == 1


def test_import_linkedin_positions_updates_owner_career_history(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert (
        CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"]).exit_code
        == 0
    )

    source = vault / "inbox" / "Positions.csv"
    source.write_text(
        "Company Name,Title,Description,Location,Started On,Finished On\n"
        "Northstar Systems,Senior Research Engineer,MPC Cryptography,,Apr 2024,\n"
        "Vertex Labs,Blockchain Developer,Worked on BLS and ECDSA libraries,,Jan 2023,Jul 2023\n"
        "Vertex Labs,Research And Development Intern,Used Golang and TypeScript APIs,Remote,Jul 2022,Dec 2022\n"
        "Harbor Tools,Blockchain Solutions Engineer,Used AWS EKS Kubernetes Docker and Solidity,,Aug 2023,Apr 2024\n",
        encoding="utf-8",
    )

    imported = import_linkedin_positions(source, vault=vault)
    imported_again = import_linkedin_positions(source, vault=vault)

    assert imported["positions"] == 4
    assert imported["companies"] == 3
    assert imported["works_at_relations"] == 1
    assert imported["former_employee_of_relations"] == 2
    assert len(imported_again["updated"]) == 4

    me_path = vault / "entities" / "people" / "me.md"
    metadata, body = read_frontmatter(me_path)
    assert metadata["properties"]["current_company"] == "Northstar Systems"
    assert metadata["properties"]["current_role"] == "Senior Research Engineer"
    assert metadata["properties"]["current_position_started_on"] == "2024-04"
    assert body.count("<!-- synapse:linkedin-positions:start -->") == 1
    assert "[[northstar-systems|Northstar Systems]]" in body
    assert body.count("## LinkedIn Positions") == 1

    relations = metadata["relations"]
    works_at = [
        relation
        for relation in relations
        if relation["type"] == "works_at" and relation["properties"]["role"] == "Senior Research Engineer"
    ]
    assert len(works_at) == 1
    assert "positions" not in works_at[0]["properties"]
    assert "skills" not in works_at[0]["properties"]
    assert "location" not in works_at[0]["properties"]
    assert works_at[0]["properties"]["role"] == "Senior Research Engineer"
    assert works_at[0]["properties"]["started_on"] == "2024-04"

    former_employees = [
        relation
        for relation in relations
        if relation["type"] == "former_employee_of"
    ]
    assert len(former_employees) == 2
    for r in former_employees:
        assert "positions" not in r["properties"]
        assert "skills" not in r["properties"]
        assert "location" not in r["properties"]

    assert "Skills: MPC Cryptography" in body
    assert "Skills: BLS Digital Signatures, ECDSA" in body

    assert (vault / "entities" / "companies" / "northstar-systems.md").exists()
    assert (vault / "entities" / "companies" / "harbor-tools.md").exists()


def test_import_linkedin_connections_keys_identity_on_linkedin_url(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault)]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        # Two different people who share a name but have distinct profile URLs.
        writer.writerow(
            ["Jordan", "Kim", "https://www.linkedin.com/in/jordan-one", "", "HugoHub", "Product Intern", "11 Dec 2024"]
        )
        writer.writerow(
            ["Jordan", "Kim", "https://www.linkedin.com/in/jordan-two", "", "Acme", "Camera Engineer", "01 Jan 2025"]
        )
        # A genuine duplicate of the first, only differing by a trailing slash.
        writer.writerow(
            ["Jordan", "Kim", "https://www.linkedin.com/in/jordan-one/", "", "HugoHub", "Product Intern", "11 Dec 2024"]
        )

    imported = import_linkedin_connections(source, vault=vault)

    assert imported["connections"] == 3
    people = sorted((vault / "entities" / "people").glob("jordan*.md"))
    # Two distinct URLs => two people; the trailing-slash duplicate merges in.
    assert len(people) == 2
    texts = [path.read_text(encoding="utf-8") for path in people]
    assert any("jordan-one" in text for text in texts)
    assert any("jordan-two" in text for text in texts)
    # No file should have stacked two different companies onto one person.
    for text in texts:
        assert text.count("type: works_at") == 1


def test_import_linkedin_connections_links_owner_and_cleans_companies(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(["Ann", "Lee", "https://www.linkedin.com/in/ann", "", "Google", "SWE", "25 May 2026"])
        # Legal-suffix variant of the same company => one canonical node.
        writer.writerow(["Ben", "Ng", "https://www.linkedin.com/in/ben", "", "Google LLC", "PM", "26 May 2026"])
        # Placeholder employer => no company node, no works_at edge.
        writer.writerow(["Cy", "Park", "https://www.linkedin.com/in/cy", "", "Stealth Startup", "Founder", "27 May 2026"])
        # Self-named employer => raw context only, not a separate company node.
        writer.writerow(["Dana", "Owner", "https://www.linkedin.com/in/dana", "", "Dana Owner", "Founder", "28 May 2026"])

    imported = import_linkedin_connections(source, vault=vault)

    # "Google" and "Google LLC" collapse; "Stealth Startup" is dropped.
    assert imported["companies"] == 1
    assert imported["knows_edges"] == 4
    assert "Stealth Startup" in imported["placeholder_companies_skipped"]
    assert "Dana Owner" in imported["placeholder_companies_skipped"]
    assert len(list((vault / "entities" / "companies").glob("*.md"))) == 1

    google = next((vault / "entities" / "companies").glob("*.md")).read_text(encoding="utf-8")
    assert "Google LLC" in google  # variant retained as an alias

    # Every connection is linked to the owner, and the body carries wikilinks
    # (so Obsidian's graph renders the network) to the owner and the company.
    ann = (vault / "entities" / "people" / "ann-lee.md").read_text(encoding="utf-8")
    assert "type: knows" in ann
    assert "target: me" in ann
    assert "channel: linkedin" in ann
    assert "LinkedIn connection of [[me|Owner]]." in ann
    assert "[[google|Google]]" in ann

    # The placeholder person keeps the raw string but has no works_at edge.
    cy = (vault / "entities" / "people" / "cy-park.md").read_text(encoding="utf-8")
    assert "current_company: Stealth Startup" in cy
    assert "type: works_at" not in cy
    assert "type: knows" in cy

    dana = (vault / "entities" / "people" / "dana-owner.md").read_text(encoding="utf-8")
    assert "current_company: Dana Owner" in dana
    assert "type: works_at" not in dana
    assert "[[dana-owner|Dana Owner]]" not in dana


def test_normalize_url_ignores_scheme_and_www() -> None:
    from synapse.importers import _normalize_url

    canonical = _normalize_url("https://www.linkedin.com/in/ann")
    assert canonical == "linkedin.com/in/ann"
    assert _normalize_url("www.linkedin.com/in/ann/") == canonical
    assert _normalize_url("http://linkedin.com/in/ann") == canonical


def test_import_dedupes_people_across_url_scheme_variants(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault)]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(["Ann", "Lee", "https://www.linkedin.com/in/ann", "", "Acme", "SWE", "25 May 2026"])
        # Same profile, scheme-less form (as other LinkedIn exports write it).
        writer.writerow(["Ann", "Lee", "www.linkedin.com/in/ann", "", "Acme", "SWE", "25 May 2026"])

    import_linkedin_connections(source, vault=vault)

    assert len(list((vault / "entities" / "people").glob("ann-lee*.md"))) == 1


def test_import_preserves_ambiguous_company_qualifiers_and_drops_nda_placeholder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault)]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(["A", "One", "https://www.linkedin.com/in/a1", "", "Northstar Labs", "SWE", "1 Jan 2026"])
        writer.writerow(["B", "Two", "https://www.linkedin.com/in/b2", "", "Northstar Labs Europe", "SWE", "1 Jan 2026"])
        writer.writerow(["C", "Three", "https://www.linkedin.com/in/c3", "", "NDA", "Engineer", "1 Jan 2026"])
        writer.writerow(["D", "Four", "https://www.linkedin.com/in/d4", "", "NDA FinTech", "Analyst", "1 Jan 2026"])

    import_linkedin_connections(source, vault=vault)

    companies = {p.name for p in (vault / "entities" / "companies").glob("*.md")}
    # Ambiguous regional qualifiers stay distinct; "NDA" is a placeholder (no node).
    assert "northstar-labs.md" in companies
    assert "northstar-labs-europe.md" in companies
    assert "nda.md" not in companies
    assert "nda-fintech.md" in companies  # a real company, kept


def test_recruiter_tagging_is_specific(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault)]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(["Tina", "T", "https://www.linkedin.com/in/tina", "", "Acme", "Technical Recruiter", "1 Jan 2026"])
        writer.writerow(["Pam", "P", "https://www.linkedin.com/in/pam", "", "Acme", "People Operations Manager", "1 Jan 2026"])
        writer.writerow(["Hank", "H", "https://www.linkedin.com/in/hank", "", "Acme", "HR Business Partner", "1 Jan 2026"])

    import_linkedin_connections(source, vault=vault)

    tina = (vault / "entities" / "people" / "tina-t.md").read_text(encoding="utf-8")
    pam = (vault / "entities" / "people" / "pam-p.md").read_text(encoding="utf-8")
    hank = (vault / "entities" / "people" / "hank-h.md").read_text(encoding="utf-8")
    assert "- recruiter" in tina  # true recruiter, tagged
    assert "- recruiter" in pam   # people operations is now a positive token, tagged
    assert "- recruiter" in hank  # HR is now a positive token, tagged


def test_is_recruiter_title_table() -> None:
    # True positives:
    assert is_recruiter_title("Technical Recruiter")
    assert is_recruiter_title("Talent Acquisition Manager")
    assert is_recruiter_title("Sourcing Specialist")
    assert is_recruiter_title("Head of Staffing")
    assert is_recruiter_title("People Ops Partner")
    assert is_recruiter_title("VP of People Operations")
    assert is_recruiter_title("HR Director")
    assert is_recruiter_title("Headhunter")
    
    # False positives from generic matches (should be false due to negative keywords):
    assert not is_recruiter_title("Talent Engineer")
    assert not is_recruiter_title("Recruiting Founder")
    assert not is_recruiter_title("HR CEO")
    assert not is_recruiter_title("Sourcing Scientist")
    
    # False negatives/completely other roles:
    assert not is_recruiter_title("Software Engineer")
    assert not is_recruiter_title("Product Manager")


def test_body_wikilinks_resolve_without_unresolved_warnings(tmp_path: Path) -> None:
    from synapse.index import reindex

    vault = tmp_path / "vault"
    assert CliRunner().invoke(app, ["init", "--path", str(vault)]).exit_code == 0

    source = vault / "inbox" / "Connections.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
        )
        writer.writerow(["Ann", "Lee", "https://www.linkedin.com/in/ann", "", "Acme Labs", "SWE", "1 Jan 2026"])

    import_linkedin_connections(source, vault=vault)
    result = reindex(vault, full=True)

    # The [[me|...]] and [[acme-labs|Acme Labs]] wikilinks must resolve by slug,
    # not leave "Unresolved weak link" warnings for every connection.
    messages = [issue.message for issue in result.issues]
    assert not any("Unresolved weak link" in m for m in messages), messages


def test_import_linkedin_connections_preserves_existing_curated_properties(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = CliRunner().invoke(app, ["init", "--path", str(vault)])
    assert result.exit_code == 0

    person = vault / "entities" / "people" / "mira-shah.md"
    person.write_text(
        """---
id: 01J000000000000000000000AA
type: person
name: Mira Shah
aliases: []
review_status: verified
tags:
  - key_contact
relations: []
properties:
  current_company: Curated Company
  current_role: Curated Role
---

# Mira Shah
""",
        encoding="utf-8",
    )

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
                "Imported Company",
                "Imported Role",
                "2024-01-02",
            ]
        )

    import_linkedin_connections(source, vault=vault)

    person_text = person.read_text(encoding="utf-8")
    assert "current_company: Curated Company" in person_text
    assert "current_role: Curated Role" in person_text
    assert "linkedin_url: https://www.linkedin.com/in/mira" in person_text
    assert "- key_contact" in person_text
    assert "- linkedin" in person_text
