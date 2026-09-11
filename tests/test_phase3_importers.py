from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.importers import (
    import_linkedin_endorsements_given,
    import_linkedin_endorsements_received,
    import_linkedin_events,
    import_linkedin_messages,
    parse_linkedin_endorsements_csv,
    parse_linkedin_events_csv,
    parse_linkedin_messages_csv,
)
from synapse.util import read_frontmatter, write_frontmatter


def test_parse_linkedin_messages_csv(tmp_path: Path) -> None:
    source = tmp_path / "messages.csv"
    source.write_text(
        '"CONVERSATION ID","CONVERSATION TITLE","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","SUBJECT","CONTENT","FOLDER","ATTACHMENTS"\n'
        '"123","","Jordan Lee","https://linkedin.com/in/jordan","Taylor Morgan","https://linkedin.com/in/taylor","2026-03-28 08:30:12 UTC","","Hello Taylor","INBOX",""\n'
        '"123","","Taylor Morgan","https://linkedin.com/in/taylor","Jordan Lee","https://linkedin.com/in/jordan","2026-03-28 09:30:12 UTC","","Hey Jordan","INBOX",""\n',
        encoding="utf-8",
    )

    msgs, warnings = parse_linkedin_messages_csv(source)
    assert len(msgs) == 2
    assert msgs[0].conversation_id == "123"
    assert msgs[0].from_name == "Jordan Lee"
    assert msgs[0].content == "Hello Taylor"
    assert msgs[1].content == "Hey Jordan"


def test_import_linkedin_messages_basic(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor Morgan"])

    source = vault / "inbox" / "messages.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        '"CONVERSATION ID","CONVERSATION TITLE","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","SUBJECT","CONTENT","FOLDER","ATTACHMENTS"\n'
        '"123","","Jordan Lee","https://linkedin.com/in/jordan","Taylor Morgan","https://linkedin.com/in/taylor","2026-03-28 08:30:12 UTC","","Hello Taylor","INBOX",""\n'
        '"123","","Taylor Morgan","https://linkedin.com/in/taylor","Jordan Lee","https://linkedin.com/in/jordan","2026-03-28 09:30:12 UTC","","Hey Jordan","INBOX",""\n',
        encoding="utf-8",
    )

    res = import_linkedin_messages(source, vault=vault)
    assert res["conversations"] == 1
    assert res["skipped_promotions"] == 0
    assert len(res["created"]) == 2  # Jordan (Person) and Conversation

    conv_path = vault / "entities" / "conversations" / "conversation-with-jordan-lee.md"
    assert conv_path.exists()

    metadata, body = read_frontmatter(conv_path)
    assert metadata["type"] == "conversation"
    assert metadata["properties"]["conversation_id"] == "123"
    assert metadata["properties"]["message_count"] == 2
    assert "Hello Taylor" in body
    assert "Hey Jordan" in body
    assert body.count("## Chat Log") == 1
    assert body.count("<!-- synapse:linkedin-chat-log:start -->") == 1
    assert body.count("<!-- synapse:linkedin-chat-log:end -->") == 1
    start = body.index("<!-- synapse:linkedin-chat-log:start -->")
    chat_log = body.index("## Chat Log")
    end = body.index("<!-- synapse:linkedin-chat-log:end -->")
    assert start < chat_log < end

    # Verify relations are now on the conversation file with direction: incoming
    conv_meta, _ = read_frontmatter(conv_path)
    relations = conv_meta.get("relations", [])
    me_meta, _ = read_frontmatter(vault / "entities" / "people" / "me.md")
    jordan_meta, _ = read_frontmatter(vault / "entities" / "people" / "jordan-lee.md")
    assert any(r["type"] == "participated_in" and r["target"] == me_meta["id"] and r.get("direction") == "incoming" for r in relations)
    assert any(r["type"] == "participated_in" and r["target"] == jordan_meta["id"] and r.get("direction") == "incoming" for r in relations)

    # Verify relations in database
    from synapse.index import all_relations, connect
    conn = connect(vault)
    try:
        db_rels = all_relations(conn)
        participated = [r for r in db_rels if r["type"] == "participated_in"]
        assert len(participated) == 2
        assert any(r["from_id"] == me_meta["id"] and r["to_id"] == metadata["id"] for r in participated)
        assert any(r["from_id"] == jordan_meta["id"] and r["to_id"] == metadata["id"] for r in participated)
    finally:
        conn.close()


def test_import_linkedin_messages_preserves_comma_in_single_recipient_name(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor Morgan"])
    source = vault / "inbox" / "messages.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        '"CONVERSATION ID","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","CONTENT"\n'
        '"123","Taylor Morgan","https://linkedin.com/in/taylor","Ada Example, Ph.D.","https://linkedin.com/in/ada","2026-03-28 08:30:12 UTC","Hello"\n'
        '"123","Ada Example, Ph.D.","https://linkedin.com/in/ada","Taylor Morgan","https://linkedin.com/in/taylor","2026-03-28 09:30:12 UTC","Hi"\n',
        encoding="utf-8",
    )

    import_linkedin_messages(source, vault=vault)

    assert not (vault / "entities" / "people" / "ph-d.md").exists()
    conv = vault / "entities" / "conversations" / "conversation-with-ada-example-ph-d.md"
    metadata, body = read_frontmatter(conv)
    assert metadata["name"] == "Conversation with Ada Example, Ph.D."
    assert metadata["properties"]["participants"] == ["Taylor Morgan", "Ada Example, Ph.D."]
    assert body.startswith("# Conversation with Ada Example, Ph.D.\n")

    person = vault / "entities" / "people" / "ada-example-ph-d.md"
    person_metadata, person_body = read_frontmatter(person)
    person_metadata["review_status"] = "verified"
    person_metadata["provenance"] = {
        "source_file": "profile.pdf",
        "extracted_by": "deterministic:linkedin-profile-pdf",
        "extracted_at": "2026-03-29T00:00:00Z",
    }
    write_frontmatter(person, person_metadata, person_body)
    conversation_before = conv.read_bytes()

    import_linkedin_messages(source, vault=vault)

    person_metadata, _ = read_frontmatter(person)
    assert person_metadata["review_status"] == "verified"
    assert person_metadata["provenance"]["source_file"] == "profile.pdf"
    assert conv.read_bytes() == conversation_before


def test_import_linkedin_messages_reconciles_removed_participant(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor Morgan"])
    source = vault / "inbox" / "messages.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    header = '"CONVERSATION ID","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","CONTENT"\n'
    source.write_text(
        header
        + '"123","Taylor Morgan","https://linkedin.com/in/taylor","Ada Example, Extra Person","https://linkedin.com/in/ada,https://linkedin.com/in/extra","2026-03-28 08:30:12 UTC","Hello"\n',
        encoding="utf-8",
    )
    import_linkedin_messages(source, vault=vault)

    source.write_text(
        header
        + '"123","Taylor Morgan","https://linkedin.com/in/taylor","Ada Example","https://linkedin.com/in/ada","2026-03-28 08:30:12 UTC","Hello"\n',
        encoding="utf-8",
    )
    import_linkedin_messages(source, vault=vault)

    conv = vault / "entities" / "conversations" / "conversation-with-ada-example-extra-person.md"
    metadata, body = read_frontmatter(conv)
    assert metadata["name"] == "Conversation with Ada Example"
    assert metadata["properties"]["participants"] == ["Taylor Morgan", "Ada Example"]
    participant_targets = {
        relation["target"]
        for relation in metadata["relations"]
        if relation["type"] == "participated_in"
    }
    assert len(participant_targets) == 2
    assert body.startswith("# Conversation with Ada Example\n")


def test_import_linkedin_messages_comma_still_separates_multiple_recipients(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor Morgan"])
    source = vault / "inbox" / "messages.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        '"CONVERSATION ID","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","CONTENT"\n'
        '"123","Taylor Morgan","https://linkedin.com/in/taylor","Ada Example,LinkedIn Member","https://linkedin.com/in/ada","2026-03-28 08:30:12 UTC","Hello"\n',
        encoding="utf-8",
    )

    import_linkedin_messages(source, vault=vault)

    conv = vault / "entities" / "conversations" / "conversation-with-ada-example-linkedin-member.md"
    metadata, _ = read_frontmatter(conv)
    assert metadata["properties"]["participants"] == [
        "Taylor Morgan",
        "Ada Example",
        "LinkedIn Member",
    ]


def test_import_linkedin_messages_filtering(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor Morgan"])

    source = vault / "inbox" / "messages.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        '"CONVERSATION ID","CONVERSATION TITLE","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","SUBJECT","CONTENT","FOLDER","ATTACHMENTS"\n'
        # Ad 1: contains spinmail-quill-editor
        '"101","","Ad Sender","","Taylor Morgan","","2026-03-28 08:30:12 UTC","","<p class=""spinmail-quill-editor"">Ad Content</p>","INBOX",""\n'
        # Ad 2: contains %FIRSTNAME%
        '"102","","Ad Sender 2","","Taylor Morgan","","2026-03-28 08:30:12 UTC","","Hi %FIRSTNAME%, join Example Institute","INBOX",""\n'
        # Ad 3: single message with promo keyword and no URL
        '"103","","Event Sponsor","","Taylor Morgan","","2026-03-28 08:30:12 UTC","","Stream the must-attend event online","INBOX",""\n'
        # Blocked User: LinkedIn Member, real conversation (length > 1), should NOT be filtered
        '"104","","LinkedIn Member","","Taylor Morgan","","2026-03-28 08:30:12 UTC","","Hey how are you","INBOX",""\n'
        '"104","","Taylor Morgan","","LinkedIn Member","","2026-03-28 08:40:12 UTC","","Doing fine, who is this?","INBOX",""\n',
        encoding="utf-8",
    )

    res = import_linkedin_messages(source, vault=vault)
    assert res["conversations"] == 1  # only conversation 104
    assert res["skipped_promotions"] == 3  # 101, 102, 103

    # LinkedIn Member should exist as a placeholder person
    lm_path = vault / "entities" / "people" / "linkedin-member.md"
    assert lm_path.exists()

    conv_path = vault / "entities" / "conversations" / "conversation-with-linkedin-member.md"
    assert conv_path.exists()


def test_parse_linkedin_endorsements_csv(tmp_path: Path) -> None:
    source = tmp_path / "endorsements.csv"
    source.write_text(
        "Endorsement Date,Skill Name,Endorser First Name,Endorser Last Name,Endorser Public Url,Endorsement Status\n"
        "2021/09/01 12:41:31 UTC,Databases,Casey,Nguyen,www.linkedin.com/in/casey,ACCEPTED\n",
        encoding="utf-8",
    )

    ends, warnings = parse_linkedin_endorsements_csv(source)
    assert len(ends) == 1
    assert ends[0].skill_name == "Databases"
    assert ends[0].first_name == "Casey"
    assert ends[0].last_name == "Nguyen"


def test_import_linkedin_endorsements_received(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    source = vault / "inbox" / "endorsements.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Endorsement Date,Skill Name,Endorser First Name,Endorser Last Name,Endorser Public Url,Endorsement Status\n"
        "2021/09/01 12:41:31 UTC,Databases,Casey,Nguyen,www.linkedin.com/in/casey,ACCEPTED\n",
        encoding="utf-8",
    )

    res = import_linkedin_endorsements_received(source, vault=vault)
    assert res["endorsements"] == 1

    casey_path = vault / "entities" / "people" / "casey-nguyen.md"
    assert casey_path.exists()
    h_meta, h_body = read_frontmatter(casey_path)
    assert any(
        rel["type"] == "endorsed_skill" and rel["properties"]["skill_name"] == "Databases"
        for rel in h_meta.get("relations", [])
    )
    assert "Endorsed [[me|Taylor]] for **Databases**" in h_body

    me_meta, me_body = read_frontmatter(vault / "entities" / "people" / "me.md")
    assert "Databases" in me_body
    assert "Casey Nguyen" in me_body


def test_import_linkedin_endorsements_given(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    source = vault / "inbox" / "endorsements.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Endorsement Date,Skill Name,Endorsee First Name,Endorsee Last Name,Endorsee Public Url,Endorsement Status\n"
        "2021/05/21 15:13:21 UTC,C++,Avery,Chen,www.linkedin.com/in/avery,ACCEPTED\n",
        encoding="utf-8",
    )

    res = import_linkedin_endorsements_given(source, vault=vault)
    assert res["endorsements"] == 1

    avery_path = vault / "entities" / "people" / "avery-chen.md"
    assert avery_path.exists()
    k_meta, k_body = read_frontmatter(avery_path)
    assert "C++" in k_body
    assert "me|Taylor" in k_body

    me_meta, me_body = read_frontmatter(vault / "entities" / "people" / "me.md")
    assert any(
        rel["type"] == "endorsed_skill" and rel["properties"]["skill_name"] == "C++"
        for rel in me_meta.get("relations", [])
    )
    assert "Avery Chen" in me_body


def test_parse_linkedin_events_csv(tmp_path: Path) -> None:
    source = tmp_path / "events.csv"
    source.write_text(
        "Event Name,Event Time,Status,External Url\n"
        "Example Tech Summit 2024,\"Apr 08, 2024 07:00 AM - Apr 08, 2024 05:00 PM\",RELINQUISHED,\n",
        encoding="utf-8",
    )

    events, warnings = parse_linkedin_events_csv(source)
    assert len(events) == 1
    assert events[0].name == "Example Tech Summit 2024"
    assert events[0].status == "RELINQUISHED"


def test_import_linkedin_events(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    CliRunner().invoke(app, ["init", "--path", str(vault), "--owner-name", "Taylor"])

    source = vault / "inbox" / "events.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Event Name,Event Time,Status,External Url\n"
        "Example Tech Summit 2024,\"Apr 08, 2024 07:00 AM - Apr 08, 2024 05:00 PM\",RELINQUISHED,\n",
        encoding="utf-8",
    )

    res = import_linkedin_events(source, vault=vault)
    assert res["events"] == 1

    event_path = vault / "entities" / "events" / "example-tech-summit-2024.md"
    assert event_path.exists()
    ev_meta, ev_body = read_frontmatter(event_path)
    assert ev_meta["type"] == "event"
    assert ev_meta["properties"]["status"] == "RELINQUISHED"

    me_meta, me_body = read_frontmatter(vault / "entities" / "people" / "me.md")
    assert any(
        rel["type"] == "attended" and rel["target"] == ev_meta["id"]
        for rel in me_meta.get("relations", [])
    )
    assert "example-tech-summit-2024" in me_body
