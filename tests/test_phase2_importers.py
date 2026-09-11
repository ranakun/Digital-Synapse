from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.importers import (
    import_linkedin_invitations,
    import_linkedin_job_applications,
    import_linkedin_saved_jobs,
    parse_linkedin_invitations_csv,
    parse_linkedin_job_applications_csv,
    parse_linkedin_saved_jobs_csv,
)
from synapse.util import read_frontmatter


def test_parse_linkedin_saved_jobs_csv(tmp_path: Path) -> None:
    source = tmp_path / "Saved Jobs.csv"
    source.write_text(
        "Saved Date,Job Url,Job Title,Company Name\n"
        "\"3/8/26, 4:55 AM\",http://www.linkedin.com/jobs/view/4382380620,Software Engineer,Nexus Consulting\n",
        encoding="utf-8",
    )

    saved_jobs, warnings = parse_linkedin_saved_jobs_csv(source)
    assert len(saved_jobs) == 1
    assert saved_jobs[0].company_name == "Nexus Consulting"
    assert saved_jobs[0].job_title == "Software Engineer"
    assert saved_jobs[0].saved_date == "2026-03-08 04:55"
    assert saved_jobs[0].job_url == "http://www.linkedin.com/jobs/view/4382380620"


def test_import_linkedin_saved_jobs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    source = vault / "inbox" / "Saved_Jobs.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Saved Date,Job Url,Job Title,Company Name\n"
        "\"12/25/25, 10:21 AM\",http://www.linkedin.com/jobs/view/4285056750,Software Engineer,OpenAI\n",
        encoding="utf-8",
    )

    result = import_linkedin_saved_jobs(source, vault=vault)
    assert result["saved_jobs"] == 1
    assert len(result["created"]) == 2  # Company (OpenAI) and Opportunity (Software Engineer at OpenAI)

    # Verify opportunity file
    opp_file = vault / "entities" / "opportunities" / "software-engineer-at-openai.md"
    assert opp_file.exists()
    metadata, body = read_frontmatter(opp_file)
    assert metadata["type"] == "opportunity"
    assert metadata["properties"]["role"] == "Software Engineer"
    assert metadata["properties"]["company"] == "OpenAI"
    assert metadata["properties"]["status"] == "saved"
    assert metadata["properties"]["saved_at"] == "2025-12-25 10:21"
    assert "OpenAI" in body

    # Verify company file
    company_file = vault / "entities" / "companies" / "openai.md"
    assert company_file.exists()

    # Verify me.md does not have targets relation
    me_file = vault / "entities" / "people" / "me.md"
    me_meta, _ = read_frontmatter(me_file)
    assert not any(r["type"] == "targets" and r["target"] == metadata["id"] for r in me_meta.get("relations", []))

    # Verify opportunity file has targets relation with direction: incoming
    opp_meta, _ = read_frontmatter(opp_file)
    opp_relations = opp_meta.get("relations", [])
    assert any(r["type"] == "targets" and r["target"] == me_meta["id"] and r.get("direction") == "incoming" for r in opp_relations)

    # Verify relation in database
    from synapse.index import all_relations, connect
    conn = connect(vault)
    try:
        db_rels = all_relations(conn)
        targets = [r for r in db_rels if r["type"] == "targets" and r["from_id"] == me_meta["id"] and r["to_id"] == metadata["id"]]
        assert len(targets) == 1
    finally:
        conn.close()


def test_parse_linkedin_job_applications_csv(tmp_path: Path) -> None:
    source = tmp_path / "Job Applications.csv"
    source.write_text(
        "Application Date,Contact Email,Contact Phone Number,Company Name,Job Title,Job Url,Resume Name,Question And Answers\n"
        "\"7/6/23, 2:20 PM\",applicant@example.test,12345,Northstar Labs,Full-stack Blockchain Developer,http://url,CandidateResume.pdf,What is your total experience?:1 | What is your expected annual CTC?:14\n",
        encoding="utf-8",
    )

    apps, warnings = parse_linkedin_job_applications_csv(source)
    assert len(apps) == 1
    assert apps[0].company_name == "Northstar Labs"
    assert apps[0].job_title == "Full-stack Blockchain Developer"
    assert apps[0].application_date == "2023-07-06 14:20"
    assert apps[0].resume_name == "CandidateResume.pdf"
    assert "total experience?:1" in apps[0].question_and_answers


def test_import_linkedin_job_applications(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    source = vault / "inbox" / "Job_Applications.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Application Date,Contact Email,Contact Phone Number,Company Name,Job Title,Job Url,Resume Name,Question And Answers\n"
        "\"7/6/23, 2:20 PM\",applicant@example.test,+15550100,Northstar Labs,Full-stack Blockchain Developer,http://url,CandidateResume.pdf,Experience:1 | Expected CTC:14\n",
        encoding="utf-8",
    )

    result = import_linkedin_job_applications(source, vault=vault)
    assert result["job_applications"] == 1

    opp_file = vault / "entities" / "opportunities" / "full-stack-blockchain-developer-at-northstar-labs.md"
    assert opp_file.exists()
    metadata, body = read_frontmatter(opp_file)
    assert metadata["properties"]["status"] == "applied"
    assert metadata["properties"]["applied_on"] == "2023-07-06 14:20"
    assert metadata["properties"]["contact_email"] == "applicant@example.test"
    assert metadata["properties"]["contact_phone"] == "+15550100"
    assert metadata["properties"]["questions_and_answers"]["Experience"] == "1"
    assert "Expected CTC" in body

    # Verify me.md does not have targets relation
    me_file = vault / "entities" / "people" / "me.md"
    me_meta, _ = read_frontmatter(me_file)
    assert not any(r["type"] == "targets" and r["target"] == metadata["id"] for r in me_meta.get("relations", []))

    # Verify opportunity file has targets relation with direction: incoming
    opp_relations = metadata.get("relations", [])
    assert any(r["type"] == "targets" and r["target"] == me_meta["id"] and r.get("direction") == "incoming" for r in opp_relations)

    # Verify relation in database
    from synapse.index import all_relations, connect
    conn = connect(vault)
    try:
        db_rels = all_relations(conn)
        targets = [r for r in db_rels if r["type"] == "targets" and r["from_id"] == me_meta["id"] and r["to_id"] == metadata["id"]]
        assert len(targets) == 1
    finally:
        conn.close()


def test_parse_linkedin_invitations_csv(tmp_path: Path) -> None:
    source = tmp_path / "Invitations.csv"
    source.write_text(
        "From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl\n"
        "Morgan Patel,Taylor Morgan,\"3/5/26, 1:45 AM\",Building something new,INCOMING,https://linkedin.com/in/morgan,https://linkedin.com/in/taylor\n",
        encoding="utf-8",
    )

    invs, warnings = parse_linkedin_invitations_csv(source)
    assert len(invs) == 1
    assert invs[0].from_name == "Morgan Patel"
    assert invs[0].direction == "INCOMING"
    assert invs[0].message == "Building something new"
    assert invs[0].sent_at == "2026-03-05 01:45"


def test_import_linkedin_invitations(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    # First add a connection for Morgan to simulate resolving an existing person.
    source_conn = vault / "inbox" / "Connections.csv"
    source_conn.parent.mkdir(parents=True, exist_ok=True)
    source_conn.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Morgan,Patel,https://www.linkedin.com/in/morgan,morgan@example.test,X,Founder,5 May 2026\n",
        encoding="utf-8",
    )
    CliRunner().invoke(app, ["import-linkedin-connections", str(source_conn), "--vault", str(vault)])

    # Run invitations import
    source_inv = vault / "inbox" / "Invitations.csv"
    source_inv.write_text(
        "From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl\n"
        "Morgan Patel,Taylor Morgan,\"3/5/26, 1:45 AM\",Hey Taylor! Building something new.,INCOMING,https://www.linkedin.com/in/morgan,https://www.linkedin.com/in/taylor\n",
        encoding="utf-8",
    )

    result = import_linkedin_invitations(source_inv, vault=vault)
    assert result["invitations"] == 1
    assert len(result["updated"]) == 1  # Morgan should be updated, not created as duplicate
    assert len(result["created"]) == 0

    morgan_file = vault / "entities" / "people" / "morgan-patel.md"
    assert morgan_file.exists()
    metadata, body = read_frontmatter(morgan_file)
    assert "Hey Taylor! Building something new." in body
