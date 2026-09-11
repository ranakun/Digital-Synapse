from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.importers import (
    import_linkedin_education,
    import_linkedin_recommendations_given,
    import_linkedin_recommendations_received,
    import_linkedin_skills,
    parse_linkedin_education_csv,
    parse_linkedin_recommendations_csv,
    parse_linkedin_skills_csv,
)
from synapse.util import read_frontmatter


def test_parse_linkedin_education_csv(tmp_path: Path) -> None:
    source = tmp_path / "Education.csv"
    source.write_text(
        "School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
        "Example State University,2019,2023,Relevant coursework,Bachelor of Technology - BTech,Football\n",
        encoding="utf-8",
    )

    education_entries, warnings = parse_linkedin_education_csv(source)
    assert len(education_entries) == 1
    assert education_entries[0].school_name == "Example State University"
    assert education_entries[0].started_on == "2019"
    assert education_entries[0].finished_on == "2023"
    assert education_entries[0].degree_name == "Bachelor of Technology - BTech"
    assert education_entries[0].activities == "Football"


def test_import_linkedin_education(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])

    source = vault / "inbox" / "Education.csv"
    source.write_text(
        "School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
        "Example State University,2019,2023,Relevant coursework,Bachelor of Technology - BTech,Football\n",
        encoding="utf-8",
    )

    res = import_linkedin_education(source, vault=vault)
    assert res["education_entries"] == 1
    assert res["schools"] == 1
    assert res["attended_relations"] == 1

    school_file = vault / "entities" / "companies" / "example-state-university.md"
    assert school_file.exists()

    me_path = vault / "entities" / "people" / "me.md"
    metadata, body = read_frontmatter(me_path)
    assert any(
        rel["type"] == "attended" and rel["properties"]["degree"] == "Bachelor of Technology - BTech"
        for rel in metadata["relations"]
    )
    assert "[[example-state-university|Example State University]]" in body
    assert "## LinkedIn Education" in body


def test_parse_linkedin_skills_csv(tmp_path: Path) -> None:
    source = tmp_path / "Skills.csv"
    source.write_text(
        "Name\n"
        "Secure MPC\n"
        "Cryptographic Research\n"
        "Secure MPC\n", # duplicate
        encoding="utf-8",
    )

    skills, warnings = parse_linkedin_skills_csv(source)
    assert len(skills) == 2
    assert skills[0].name == "Secure MPC"
    assert skills[1].name == "Cryptographic Research"
    assert any("duplicate skill" in warning for warning in warnings)


def test_import_linkedin_skills(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])

    source = vault / "inbox" / "Skills.csv"
    source.write_text(
        "Name\n"
        "Secure MPC\n"
        "Cryptographic Research\n",
        encoding="utf-8",
    )

    res = import_linkedin_skills(source, vault=vault)
    assert res["skills"] == 2
    assert res["demonstrates_skill_edges"] == 2

    assert (vault / "entities" / "skills" / "secure-mpc.md").exists()
    assert (vault / "entities" / "skills" / "cryptographic-research.md").exists()

    me_path = vault / "entities" / "people" / "me.md"
    metadata, _ = read_frontmatter(me_path)
    assert len([rel for rel in metadata["relations"] if rel["type"] == "demonstrates_skill"]) == 2


def test_parse_linkedin_recommendations_csv(tmp_path: Path) -> None:
    source = tmp_path / "Recommendations.csv"
    source.write_text(
        "First Name,Last Name,Company,Job Title,Text,Creation Date,Status\n"
        "Jordan,Lee,Horizon Labs,Engineering Lead,Taylor did excellent work,09/06/23,VISIBLE\n",
        encoding="utf-8",
    )

    recommendations, warnings = parse_linkedin_recommendations_csv(source)
    assert len(recommendations) == 1
    assert recommendations[0].first_name == "Jordan"
    assert recommendations[0].last_name == "Lee"
    assert recommendations[0].text == "Taylor did excellent work"
    assert recommendations[0].company == "Horizon Labs"


def test_import_linkedin_recommendations_received(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])

    source = vault / "inbox" / "Recommendations_Received.csv"
    source.write_text(
        "First Name,Last Name,Company,Job Title,Text,Creation Date,Status\n"
        "Jordan,Lee,Horizon Labs,Engineering Lead,Taylor did excellent work,09/06/23,VISIBLE\n",
        encoding="utf-8",
    )

    res = import_linkedin_recommendations_received(source, vault=vault)
    assert res["recommendations"] == 1

    recommender_file = vault / "entities" / "people" / "jordan-lee.md"
    assert recommender_file.exists()
    recommender_text = recommender_file.read_text(encoding="utf-8")
    assert "## LinkedIn Recommendations Given" in recommender_text
    assert "Taylor did excellent work" in recommender_text

    me_path = vault / "entities" / "people" / "me.md"
    me_text = me_path.read_text(encoding="utf-8")
    assert "## LinkedIn Recommendations Received" in me_text
    assert "Taylor did excellent work" in me_text


def test_import_linkedin_recommendations_given(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Owner"])

    source = vault / "inbox" / "Recommendations_Given.csv"
    source.write_text(
        "First Name,Last Name,Company,Job Title,Text,Creation Date,Status\n"
        "Aman,Rai,Catalyst IQ,Lead Talent Specialist,Aman is very efficient,05/09/24,VISIBLE\n",
        encoding="utf-8",
    )

    res = import_linkedin_recommendations_given(source, vault=vault)
    assert res["recommendations"] == 1

    aman_file = vault / "entities" / "people" / "aman-rai.md"
    assert aman_file.exists()
    aman_text = aman_file.read_text(encoding="utf-8")
    assert "## LinkedIn Recommendations Received" in aman_text
    assert "Aman is very efficient" in aman_text

    me_path = vault / "entities" / "people" / "me.md"
    me_text = me_path.read_text(encoding="utf-8")
    assert "## LinkedIn Recommendations Given" in me_text
    assert "Aman is very efficient" in me_text
