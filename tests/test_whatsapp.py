from __future__ import annotations

import hashlib
from pathlib import Path

from typer.testing import CliRunner

from synapse.cli import app
from synapse.config import load_config, save_config
from synapse.extractors import extract_whatsapp_chat, parse_whatsapp_messages
from synapse.index import reindex
from synapse.util import read_frontmatter, write_frontmatter
from synapse.whatsapp import import_whatsapp_chat

FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp"


def _snapshot(vault: Path) -> dict[str, str]:
    return {
        str(path.relative_to(vault)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(vault.rglob("*"))
        if path.is_file() and ".synapse" not in path.parts
    }


def test_whatsapp_parser_variants_preserve_content() -> None:
    android = (FIXTURES / "android-day-first.txt").read_text(encoding="utf-8")
    messages, warnings = parse_whatsapp_messages(android, date_order="day-first")
    assert warnings == []
    assert len(messages) == 4
    assert messages[0].timestamp == "2026-06-15T09:30:00"
    assert messages[0].content == "First line\ncontinues without indentation"
    assert messages[1].content == "This message was edited"
    assert messages[2].content == "<Media omitted>"
    assert messages[3].is_system is True

    iphone_path = FIXTURES / "iphone-month-first.txt"
    iphone = extract_whatsapp_chat(iphone_path, date_order="month-first")
    assert iphone.metadata["message_count"] == 3
    assert iphone.metadata["system_message_count"] == 1
    assert "+1 (555) 111-2222" in iphone.metadata["participants"]
    assert "This message was deleted." in iphone.text


def _init_identity_vault(vault: Path) -> None:
    assert CliRunner().invoke(
        app,
        ["init", "--path", str(vault), "--owner-name", "Owner", "--no-git"],
    ).exit_code == 0
    cfg = load_config(vault)
    cfg.setdefault("identity", {})["owner_phones"] = ["+91 90000 00000"]
    save_config(vault, cfg)
    write_frontmatter(
        vault / "entities" / "people" / "alice.md",
        {
            "id": "01J000000000000000000000W1",
            "type": "person",
            "name": "Alice Example",
            "aliases": [],
            "review_status": "verified",
            "tags": ["linkedin"],
            "relations": [],
            "properties": {
                "phones": ["+1 555 111 2222"],
                "linkedin_url": "https://linkedin.com/in/alice",
            },
        },
        "# Alice Example",
    )
    reindex(vault, full=True)


def test_whatsapp_import_overlap_identity_and_idempotency(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _init_identity_vault(vault)

    first = import_whatsapp_chat(
        FIXTURES / "overlap-initial.txt",
        chat_key="alice-career",
        chat_label="Alice career",
        vault=vault,
        date_order="day-first",
    )
    assert first["message_count"] == 2
    assert first["participants"]["+91 90000 00000"]["id"] == "me"
    assert first["participants"]["+1 (555) 111-2222"]["id"] == "01J000000000000000000000W1"

    second = import_whatsapp_chat(
        FIXTURES / "overlap-later.txt",
        chat_key="alice-career",
        chat_label="Alice career",
        vault=vault,
        date_order="day-first",
    )
    assert second["message_count"] == 3
    conversation_path = vault / second["conversation_file"]
    metadata, body = read_frontmatter(conversation_path)
    assert metadata["properties"]["message_count"] == 3
    assert body.count("Hello Alice") == 1
    assert body.count("Hello owner") == 1
    assert body.count("Here is the role description") == 1
    participant_targets = {
        relation["target"]
        for relation in metadata["relations"]
        if relation["type"] == "participated_in"
    }
    assert participant_targets == {"me", "01J000000000000000000000W1"}

    before = _snapshot(vault)
    import_whatsapp_chat(
        FIXTURES / "overlap-later.txt",
        chat_key="alice-career",
        chat_label="Alice career",
        vault=vault,
        date_order="day-first",
    )
    assert _snapshot(vault) == before


def test_whatsapp_name_only_stays_unresolved(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _init_identity_vault(vault)
    source = tmp_path / "saved-name.txt"
    source.write_text(
        "15/06/2026, 09:30 - +91 90000 00000: Hello\n"
        "15/06/2026, 09:31 - Alice Example: Hi\n",
        encoding="utf-8",
    )
    result = import_whatsapp_chat(
        source,
        chat_key="saved-name-alice",
        vault=vault,
        date_order="day-first",
    )
    unresolved_id = result["participants"]["Alice Example"]["id"]
    assert unresolved_id != "01J000000000000000000000W1"
    unresolved_path = vault / next(
        item["file_path"] for item in result["created_people"] if item["id"] == unresolved_id
    )
    metadata, _ = read_frontmatter(unresolved_path)
    assert "identity-unresolved" in metadata["tags"]
    assert result["identity_proposal"]


def test_whatsapp_filters_before_writes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _init_identity_vault(vault)
    result = import_whatsapp_chat(
        FIXTURES / "overlap-initial.txt",
        chat_key="filtered",
        vault=vault,
        since="2026-06-16",
        min_messages=2,
        date_order="day-first",
    )
    assert result["imported"] is False
    assert not (vault / "inbox" / "processed" / "whatsapp" / "filtered").exists()
