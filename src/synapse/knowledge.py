"""Canonical Markdown records and lossless legacy adapters for v2."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

import yaml

from synapse.v2_contracts import V2Error, hash_bytes, validate_payload

PROFILE = "knowledge-v2"


@lru_cache(maxsize=512)
def _parsed_parts(raw: bytes) -> tuple[dict[str, Any], str]:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("Missing entity frontmatter")
        end = text.find("\n---", 4)
        if end < 0:
            raise ValueError("Unterminated entity frontmatter")
        meta = yaml.safe_load(text[4:end])
        if not isinstance(meta, dict):
            raise ValueError("Entity metadata must be a mapping")
        return meta, text[end + 4 :].lstrip("\n")
    except (UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise V2Error("invalid-record", str(exc)) from exc


def _parts(raw: bytes) -> tuple[dict[str, Any], str]:
    # Cache parsing by exact content, never by a mutable path or current HEAD.
    # Return owned metadata so callers cannot corrupt a subsequent read.
    metadata, body = _parsed_parts(raw)
    return copy.deepcopy(metadata), body


def _body(payload: dict[str, Any]) -> str:
    sections = [
        ("Statement", payload["statement"]),
        ("Conditions and limits", payload["conditions_and_limits"]),
        ("Support", payload["support"]),
        ("Evidence", json.dumps(payload["evidence"], ensure_ascii=False, indent=2)),
    ]
    for title, key in [
        ("Counterevidence", "counterevidence"),
        ("Alternatives", "alternatives"),
        ("What would change this", "would_change_with"),
    ]:
        sections.append((title, "\n".join(f"- {x}" for x in payload[key]) or "None recorded."))
    return "\n\n".join(f"## {title}\n\n{text}" for title, text in sections) + "\n"


def encode_record(payload: dict[str, Any], *, name: str | None = None) -> bytes:
    """Render a validated record; its exact byte hash is returned on a later read.

    The self-referential wire version is excluded from canonical frontmatter.
    Rendering never supplies authority, owner endorsement or inferred evidence.
    """
    value = copy.deepcopy(payload)
    value.setdefault("version", "0" * 64)
    validate_payload("knowledge_record", value)
    value.pop("version")
    metadata = {
        "id": value["id"],
        "type": "insight",
        "name": name or value["claim_key"],
        "profile": PROFILE,
        "review_status": value["review_status"],
        "properties": {"knowledge_v2": value},
    }
    if value["lifecycle"] == "withdrawn":
        metadata["archived"] = True
    rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{rendered}\n---\n\n{_body(value)}".encode()


def decode_record(raw: bytes, *, path: str = "") -> dict[str, Any]:
    """Return the public record payload without changing any preserved bytes."""
    meta, body = _parts(raw)
    for key in ("id", "type", "name", "review_status"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise V2Error("invalid-record", f"Missing or invalid {key}", details={"path": path})
    if meta["review_status"] not in {"proposed", "verified"}:
        raise V2Error("invalid-record", "Invalid legacy attestation", details={"path": path})
    if meta.get("profile") == PROFILE:
        properties = meta.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("knowledge_v2"), dict):
            raise V2Error("invalid-record", "knowledge-v2 payload is missing")
        value = copy.deepcopy(properties["knowledge_v2"])
        if "version" in value:
            raise V2Error("invalid-record", "Canonical records cannot contain their own byte hash")
        value["version"] = hash_bytes(raw)
        validate_payload("knowledge_record", value)
        if value["id"] != meta["id"] or value["review_status"] != meta["review_status"]:
            raise V2Error("invalid-record", "Frontmatter disagrees with knowledge metadata")
        if body.strip() != _body(value).strip():
            raise V2Error("invalid-record", "Readable body disagrees with the knowledge metadata")
        if bool(meta.get("archived")) != (value["lifecycle"] == "withdrawn"):
            raise V2Error("invalid-record", "Lifecycle disagrees with archive marker")
        return value
    # Existing Markdown can be a perfectly valid entity without evidence anchors.
    # Its metadata/body are never promoted to v2 assertions by the adapter.
    return {
        "id": meta["id"],
        "version": hash_bytes(raw),
        "type": meta["type"],
        "name": meta["name"],
        "body": body,
        "review_status": meta["review_status"],
        "availability": "accepted",
        "correction_refs": [],
        "coverage_limitations": [
            "Legacy record: exact original bytes retained; universal passage anchors and premise coverage are unavailable."
        ],
    }


def validate_record_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.parts[0] != "entities"
        or candidate.suffix != ".md"
        or str(candidate) != path
    ):
        raise V2Error("invalid-path", "Records require a relative entities/*.md path")
    return path


def record_descriptor(raw: bytes, *, path: str) -> dict[str, Any]:
    validate_record_path(path)
    value = decode_record(raw, path=path)
    meta, _ = _parts(raw)
    return {
        "id": value["id"],
        "version": value["version"],
        "path": path,
        "profile": PROFILE if meta.get("profile") == PROFILE else "legacy",
        "type": meta["type"],
        "name": meta["name"],
        "availability": value["availability"],
        "review_status": value["review_status"],
        "active": not bool(meta.get("archived") or meta.get("merged_into")) and value.get("lifecycle", "current") == "current",
        "merged_into": str(meta["merged_into"]) if meta.get("merged_into") else None,
        "disposition": value.get("owner_review", {}).get("disposition", "none"),
    }


def metadata_and_body(raw: bytes) -> tuple[dict[str, Any], str]:
    """Internal compatibility seam for the existing entity parser/index."""
    return _parts(raw)
