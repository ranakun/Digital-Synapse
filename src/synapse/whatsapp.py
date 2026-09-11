"""Deterministic WhatsApp text-export importer."""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from synapse.config import load_config, resolve_vault
from synapse.extractors import WhatsAppMessage, parse_whatsapp_messages
from synapse.identity import IdentityIndex, alias_worthy, emit_candidates_proposal, norm_phone
from synapse.importers import (
    _add_relation,
    _entity_index,
    _managed_section,
    _merge_aliases,
    _merge_properties,
    _merge_tags,
    _read_or_create_entity,
)
from synapse.index import connect, reindex
from synapse.parser import entity_files
from synapse.util import normalize_name, read_frontmatter, slugify, utc_now, write_frontmatter

WHATSAPP_IMPORTER = "deterministic:whatsapp-chat"
_CHAT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,119}$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")
_MESSAGE_BLOCK_RE = re.compile(
    r"<!-- synapse:whatsapp-message:(?P<key>[0-9a-f]{64}:\d+):start -->\n"
    r"<!-- synapse:whatsapp-timestamp:(?P<timestamp>[^>]*) -->\n"
    r"(?P<body>.*?)\n"
    r"<!-- synapse:whatsapp-message:(?P=key):end -->",
    flags=re.DOTALL,
)


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _person_paths(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    by_id: dict[str, Path] = {}
    by_source_key: dict[str, Path] = {}
    for path in entity_files(root):
        metadata, _ = read_frontmatter(path)
        if metadata.get("type") != "person":
            continue
        entity_id = str(metadata.get("id") or "")
        if entity_id:
            by_id[entity_id] = path
        properties = metadata.get("properties") or {}
        if not isinstance(properties, dict):
            continue
        keys = properties.get("whatsapp_participant_keys") or []
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            by_source_key[str(key)] = path
    return by_id, by_source_key


def _source_participant_key(chat_key: str, sender: str) -> str:
    phone = norm_phone(sender) if _PHONE_RE.fullmatch(sender.strip()) else ""
    identity = f"phone:{phone}" if phone else f"name:{normalize_name(sender)}"
    return f"whatsapp:{chat_key.casefold()}:{identity}"


def _append_person_identity(
    path: Path,
    *,
    source_key: str,
    sender: str,
    phone: str,
) -> None:
    metadata, body = read_frontmatter(path)
    before = copy.deepcopy(metadata)
    properties = metadata.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    keys = properties.get("whatsapp_participant_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    properties["whatsapp_participant_keys"] = list(
        dict.fromkeys([*[str(item) for item in keys], source_key])
    )
    properties = _merge_properties(
        properties,
        {
            "phones": [phone] if phone else [],
            "source": ["whatsapp"],
        },
    )
    metadata["properties"] = properties
    if alias_worthy(sender):
        metadata["aliases"] = _merge_aliases(
            metadata.get("aliases"),
            [sender],
            str(metadata.get("name") or ""),
        )
    metadata["review_status"] = "proposed"
    metadata["tags"] = _merge_tags(metadata.get("tags"), ["whatsapp"])
    comparable_before = copy.deepcopy(before)
    comparable_after = copy.deepcopy(metadata)
    comparable_before.pop("updated_at", None)
    comparable_after.pop("updated_at", None)
    if comparable_before != comparable_after:
        metadata["updated_at"] = utc_now()
        write_frontmatter(path, metadata, body)


def _message_key(message: WhatsAppMessage, occurrence: int) -> str:
    payload = "\x1f".join(
        [
            message.timestamp,
            message.sender or "",
            message.content,
            "system" if message.is_system else "message",
        ]
    )
    return f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}:{occurrence}"


def _render_message_block(message: WhatsAppMessage, key: str) -> str:
    sender = message.sender or "system"
    content = message.content
    return "\n".join(
        [
            f"<!-- synapse:whatsapp-message:{key}:start -->",
            f"<!-- synapse:whatsapp-timestamp:{message.timestamp} -->",
            f"**{message.timestamp_text} ({sender}):**",
            content,
            f"<!-- synapse:whatsapp-message:{key}:end -->",
        ]
    ).rstrip()


def _existing_message_blocks(body: str) -> dict[str, tuple[str, str]]:
    return {
        match.group("key"): (
            match.group("timestamp"),
            match.group(0),
        )
        for match in _MESSAGE_BLOCK_RE.finditer(body)
    }


def _render_chat_log(blocks: dict[str, tuple[str, str]]) -> str:
    ordered = sorted(blocks.items(), key=lambda item: (item[1][0], item[0]))
    return "\n\n".join(["## WhatsApp Chat Log", "", *[value[1] for _, value in ordered]])


def _parse_since(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("since must use YYYY-MM-DD") from exc


def _message_date(message: WhatsAppMessage) -> date | None:
    try:
        return datetime.fromisoformat(message.timestamp).date()
    except ValueError:
        return None


def _copy_source(root: Path, source: Path, chat_key: str) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = (
        root
        / "inbox"
        / "processed"
        / "whatsapp"
        / slugify(chat_key)
        / f"{digest[:16]}-{slugify(source.stem)}.txt"
    )
    if destination.exists():
        if destination.read_bytes() != source.read_bytes():
            raise ValueError(f"Refusing to overwrite different source: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _resolve_participants(
    root: Path,
    *,
    chat_key: str,
    senders: list[str],
    explicit: dict[str, str],
    source_file: str,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], list[dict[str, str]]]:
    cfg = load_config(root)
    identity_cfg = cfg.get("identity") or {}
    owner_id = str(cfg.get("owner_entity_id") or "me")
    conn = connect(root)
    try:
        identity_index = IdentityIndex.build(
            conn,
            owner_emails=identity_cfg.get("owner_emails") or [],
            owner_phones=identity_cfg.get("owner_phones") or [],
            owner_id=owner_id,
        )
    finally:
        conn.close()

    by_id, by_source_key = _person_paths(root)
    resolved: dict[str, dict[str, str]] = {}
    unresolved: list[dict[str, Any]] = []
    created: list[dict[str, str]] = []

    for sender in senders:
        source_key = _source_participant_key(chat_key, sender)
        mapped_id = explicit.get(sender)
        if mapped_id:
            person_path = by_id.get(mapped_id)
            if person_path is None:
                raise ValueError(f"Participant mapping {sender!r} references missing person {mapped_id!r}")
            resolution_id = mapped_id
            method = "explicit"
            candidates: list[str] = []
        elif source_key in by_source_key:
            person_path = by_source_key[source_key]
            metadata, _ = read_frontmatter(person_path)
            resolution_id = str(metadata.get("id") or "")
            method = "whatsapp-key"
            candidates = []
        else:
            phone = norm_phone(sender) if _PHONE_RE.fullmatch(sender.strip()) else ""
            resolution = identity_index.resolve(
                {
                    "name": sender,
                    "phones": [phone] if phone else [],
                }
            )
            resolution_id = resolution.id or ""
            method = resolution.method
            candidates = resolution.candidates
            person_path = by_id.get(resolution_id) if resolution_id else None

        phone = norm_phone(sender) if _PHONE_RE.fullmatch(sender.strip()) else ""
        if not resolution_id:
            unresolved_name = not phone
            resolution_id, person_path, was_created = _read_or_create_entity(
                root,
                entity_type="person",
                name=sender,
                source_file=source_file,
                extractor=WHATSAPP_IMPORTER,
                properties={
                    "phones": [phone] if phone else [],
                    "whatsapp_participant_keys": [source_key],
                    "source": ["whatsapp"],
                },
                tags=["whatsapp", *(["identity-unresolved"] if unresolved_name else [])],
                body_append="Participant in an imported WhatsApp conversation.",
                existing_path=person_path,
            )
            method = "created:phone" if phone else "created:unresolved"
            by_id[resolution_id] = person_path
            by_source_key[source_key] = person_path
            if was_created:
                created.append(
                    {
                        "id": resolution_id,
                        "name": sender,
                        "file_path": str(person_path.relative_to(root)),
                    }
                )
            if candidates:
                unresolved.append(
                    {
                        "name": sender,
                        "candidates": candidates,
                        "evidence": f"WhatsApp chat {chat_key}; source {source_file}",
                    }
                )
        elif resolution_id != owner_id:
            _append_person_identity(
                person_path,
                source_key=source_key,
                sender=sender,
                phone=phone,
            )

        resolved[sender] = {
            "id": resolution_id,
            "method": method,
            "source_key": source_key,
        }

    return resolved, unresolved, created


def import_whatsapp_chat(
    source: str | Path,
    *,
    chat_key: str,
    vault: str | Path | None = None,
    since: str | None = None,
    min_messages: int = 2,
    date_order: str = "auto",
    chat_label: str | None = None,
    participant_mappings: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Import one WhatsApp export as one stable conversation entity."""
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "import_whatsapp_chat")

    if not _CHAT_KEY_RE.fullmatch(chat_key):
        raise ValueError(
            "chat_key must be 1-120 characters using letters, digits, and ._:@+-"
        )
    if min_messages < 1:
        raise ValueError("min_messages must be at least 1")
    root = resolve_vault(vault)
    source_path = Path(source).resolve()
    since_date = _parse_since(since)
    parsed, warnings = parse_whatsapp_messages(
        _read_source(source_path),
        date_order=date_order,
    )
    filtered: list[WhatsAppMessage] = []
    for message in parsed:
        message_date = _message_date(message)
        if since_date is not None:
            if message_date is None:
                warnings.append(
                    f"Message at {message.timestamp_text!r} was excluded because --since "
                    "could not evaluate its timestamp."
                )
                continue
            if message_date < since_date:
                continue
        filtered.append(message)

    if len(filtered) < min_messages:
        return {
            "chat_key": chat_key,
            "imported": False,
            "reason": f"{len(filtered)} messages after filters; minimum is {min_messages}",
            "message_count": len(filtered),
            "warnings": list(dict.fromkeys(warnings)),
        }

    processed_path = _copy_source(root, source_path, chat_key)
    source_file = processed_path.relative_to(root).as_posix()
    reindex(root)
    senders = sorted({message.sender for message in filtered if message.sender})
    resolved, unresolved, created_people = _resolve_participants(
        root,
        chat_key=chat_key,
        senders=senders,
        explicit=participant_mappings or {},
        source_file=source_file,
    )
    proposal_path = emit_candidates_proposal(root, unresolved) if unresolved else None

    cfg = load_config(root)
    owner_id = str(cfg.get("owner_entity_id") or "me")
    non_owner_names = [
        sender for sender in senders if resolved[sender]["id"] != owner_id
    ]
    display_label = chat_label or (
        non_owner_names[0] if len(non_owner_names) == 1 else chat_key
    )
    conversation_id = f"whatsapp:{chat_key.casefold()}"
    index = _entity_index(root)
    existing_path = index.get(("conversation", f"id:{conversation_id}"))
    conversation_id_value, conversation_path, conversation_created = _read_or_create_entity(
        root,
        entity_type="conversation",
        name=f"WhatsApp: {display_label}",
        source_file=source_file,
        extractor=WHATSAPP_IMPORTER,
        properties={
            "conversation_id": conversation_id,
            "chat_key": chat_key,
            "chat_label": display_label,
            "source": ["whatsapp"],
        },
        tags=["whatsapp", "conversation"],
        body_append=f"WhatsApp conversation: {display_label}.",
        existing_path=existing_path,
    )

    metadata, body = read_frontmatter(conversation_path)
    before_metadata = copy.deepcopy(metadata)
    before_body = body
    existing_blocks = _existing_message_blocks(body)
    occurrence_counts: dict[str, int] = defaultdict(int)
    for message in filtered:
        base_payload = "\x1f".join(
            [message.timestamp, message.sender or "", message.content]
        )
        base = hashlib.sha256(base_payload.encode("utf-8")).hexdigest()
        occurrence = occurrence_counts[base]
        occurrence_counts[base] += 1
        key = _message_key(message, occurrence)
        existing_blocks.setdefault(
            key,
            (message.timestamp, _render_message_block(message, key)),
        )

    body = _managed_section(
        body,
        "whatsapp-chat-log",
        _render_chat_log(existing_blocks),
    )
    properties = metadata.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    source_files = properties.get("source_files") or []
    if isinstance(source_files, str):
        source_files = [source_files]
    all_timestamps = [timestamp for timestamp, _ in existing_blocks.values() if timestamp]
    participant_labels = sorted(set(senders))
    properties.update(
        {
            "conversation_id": conversation_id,
            "chat_key": chat_key,
            "chat_label": display_label,
            "message_count": len(existing_blocks),
            "started_at": min(all_timestamps) if all_timestamps else "",
            "last_message_at": max(all_timestamps) if all_timestamps else "",
            "participants": participant_labels,
            "source_files": list(dict.fromkeys([*[str(item) for item in source_files], source_file])),
        }
    )
    properties = _merge_properties(properties, {"source": ["whatsapp"]})
    metadata["properties"] = properties
    metadata["review_status"] = "proposed"
    metadata["tags"] = _merge_tags(metadata.get("tags"), ["whatsapp", "conversation"])
    candidate_provenance = {
        **(metadata.get("provenance") or {}),
        "source_file": source_file,
        "extracted_by": WHATSAPP_IMPORTER,
    }
    metadata["provenance"] = candidate_provenance

    comparable_before = copy.deepcopy(before_metadata)
    comparable_after = copy.deepcopy(metadata)
    for candidate in (comparable_before, comparable_after):
        candidate.pop("updated_at", None)
        provenance = candidate.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("extracted_at", None)
    if comparable_after != comparable_before or body.strip() != before_body.strip():
        now = utc_now()
        metadata["updated_at"] = now
        metadata["provenance"] = {**candidate_provenance, "extracted_at": now}
        write_frontmatter(conversation_path, metadata, body)

    participant_ids = {owner_id}
    participant_ids.update(item["id"] for item in resolved.values())
    sender_by_id: dict[str, list[str]] = defaultdict(list)
    method_by_id: dict[str, list[str]] = defaultdict(list)
    for sender, resolution in resolved.items():
        sender_by_id[resolution["id"]].append(sender)
        method_by_id[resolution["id"]].append(resolution["method"])
    for participant_id in sorted(participant_ids):
        _add_relation(
            conversation_path,
            {
                "type": "participated_in",
                "target": participant_id,
                "direction": "incoming",
                "properties": {
                    "source": "whatsapp",
                    "participant_labels": sorted(sender_by_id.get(participant_id, [])),
                    "identity_match": sorted(set(method_by_id.get(participant_id, ["owner"]))),
                },
                "source": source_file,
            },
        )

    reindex(root, full=True)
    return {
        "chat_key": chat_key,
        "conversation_id": conversation_id_value,
        "conversation_file": str(conversation_path.relative_to(root)),
        "created": conversation_created,
        "message_count": len(existing_blocks),
        "imported_messages": len(filtered),
        "participants": resolved,
        "created_people": created_people,
        "identity_proposal": str(proposal_path) if proposal_path else None,
        "source_file": source_file,
        "warnings": list(dict.fromkeys(warnings)),
        "imported": True,
    }
