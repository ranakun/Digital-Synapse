"""Pure preparation and reading helpers for retained v2 source objects.

This module deliberately has no knowledge of a vault or a filesystem.  A
caller supplies the bytes to retain and, later, a reader for content addressed
objects.  The descriptor is the authority for which objects and versions may
be read; a path or a caller supplied current file never substitutes for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .util import generate_ulid, utc_now
from .v2_contracts import V2Error, hash_bytes, validate_payload, version_for

_MAX_CONTEXT_CHARACTERS = 10_000
_MAX_PAGE_CHARACTERS = 32_000
_HEX_HASH_LENGTH = 64
_COMPLETENESS = {"complete", "partial", "failed", "pending"}
_PROCESSING = {"captured", "partly-processed", "processed", "failed"}


def prepare_source(
    raw: bytes,
    *,
    origin: str,
    media_type: str = "text/plain",
    source_id: str | None = None,
    source_family_id: str | None = None,
    captured_at: str | None = None,
    text: str | None = None,
    extraction: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Prepare a source descriptor and its immutable content-addressed bytes.

    ``raw`` is always retained as the original object.  Text sources default
    to strict UTF-8 preservation.  A supplied ``text`` value is a distinct
    extracted-text object, even when it happens to represent the same source
    in a different form.
    """

    _require_bytes(raw, "raw")
    _require_nonempty_string(origin, "origin")
    _require_nonempty_string(media_type, "media_type")

    source_id = _source_id(source_id, "source_id")
    source_family_id = (
        source_id if source_family_id is None else _source_id(source_family_id, "source_family_id")
    )
    captured_at = _capture_time(captured_at)

    explicit_text = text is not None
    is_text_media = media_type.casefold().startswith("text/")
    raw_text: str | None = None
    derived_text = False
    objects: dict[str, bytes] = {}
    original_hash = _retain(raw, objects)

    pending_or_failed = (
        isinstance(extraction, Mapping)
        and extraction.get("completeness") in {"pending", "failed"}
        and text is None
    )
    if is_text_media and not pending_or_failed:
        try:
            raw_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail(
                "invalid-request",
                "text media must contain valid UTF-8",
                details={"field": "raw", "reason": "invalid-utf8", "position": exc.start},
            )
        if text is None:
            text = raw_text
            derived_text = True

    if text is not None:
        if not isinstance(text, str):
            _fail("invalid-request", "text must be a string when supplied")
        try:
            text_bytes = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            _fail(
                "invalid-request",
                "text must be valid UTF-8",
                details={"field": "text", "reason": "invalid-utf8", "position": exc.start},
            )
        if not text_bytes and not derived_text:
            _fail("invalid-request", "text must not be empty when supplied")
        text_hash = _retain(text_bytes, objects)
    else:
        text_hash = None

    extraction_value = _extraction(
        extraction,
        text_supplied=text is not None,
        is_text_media=is_text_media,
        explicit_text_differs=explicit_text and is_text_media and text != raw_text,
    )
    if text is None and extraction_value["completeness"] not in {"failed", "pending"}:
        _fail(
            "invalid-request",
            "an extraction marked complete or partial requires extracted text",
        )
    if text is not None and extraction_value["completeness"] in {"failed", "pending"}:
        _fail("invalid-request", "failed extraction cannot include extracted text")

    descriptor: dict[str, Any] = {
        "id": source_id,
        "original_hash": original_hash,
        "source_family_id": source_family_id,
        "captured_at": captured_at,
        "media_type": media_type,
        "origin": origin,
        "extraction": extraction_value,
        "processing": _processing(
            extraction_value,
            extraction_supplied=extraction is not None,
            explicit_text=explicit_text,
        ),
    }
    if text_hash is not None:
        descriptor["text_version"] = text_hash

    # The version is deliberately calculated only after all descriptor fields
    # are fixed.  version_for excludes the version field per HASHING.md.
    descriptor["version"] = version_for("source_version", descriptor)
    _validate_descriptor(descriptor)
    return descriptor, objects


def evidence_ref(
    descriptor: Mapping[str, Any],
    read_object: Callable[[str], bytes],
    byte_start: int,
    byte_end: int,
    *,
    original_locator: Mapping[str, Any] | None = None,
    speaker: str | None = None,
) -> dict[str, Any]:
    """Create an evidence reference after verifying its exact UTF-8 span."""

    descriptor_value = _validated_descriptor(descriptor)
    _require_reader(read_object)
    text_hash = descriptor_value.get("text_version")
    if not isinstance(text_hash, str):
        _fail("source-unavailable", "source has no retained extracted text")
    _read_verified(read_object, descriptor_value["original_hash"], "original")
    text_bytes = _read_verified(read_object, text_hash, "text")
    _decode_utf8(text_bytes, field="text object")
    start, end = _span_bytes(byte_start, byte_end, text_bytes)
    excerpt_hash = hash_bytes(text_bytes[start:end])

    reference: dict[str, Any] = {
        "source_id": descriptor_value["id"],
        "source_version": descriptor_value["version"],
        "text_version": text_hash,
        "byte_start": start,
        "byte_end": end,
        "excerpt_hash": excerpt_hash,
        "source_family_id": descriptor_value["source_family_id"],
    }
    if original_locator is not None:
        if not isinstance(original_locator, Mapping):
            _fail("invalid-span", "original_locator must be an object")
        reference["original_locator"] = dict(original_locator)
    if speaker is not None:
        _require_nonempty_string(speaker, "speaker")
        reference["speaker"] = speaker
    _validate_evidence_shape(reference)
    return reference


def read_passage(
    descriptor: Mapping[str, Any],
    evidence: Mapping[str, Any],
    read_object: Callable[[str], bytes],
    *,
    context_characters: int = 300,
) -> dict[str, Any]:
    """Read and verify one exact evidence span and bounded character context."""

    descriptor_value = _validated_descriptor(descriptor)
    _require_reader(read_object)
    if not isinstance(context_characters, int) or isinstance(context_characters, bool):
        _fail("invalid-request", "context_characters must be an integer")
    if context_characters < 0 or context_characters > _MAX_CONTEXT_CHARACTERS:
        _fail("invalid-request", "context_characters is outside the supported range")

    evidence_value = _validated_evidence(evidence, descriptor_value)
    text_hash = descriptor_value.get("text_version")
    if not isinstance(text_hash, str):
        _fail("source-unavailable", "source has no retained extracted text")
    _read_verified(read_object, descriptor_value["original_hash"], "original")
    text_bytes = _read_verified(read_object, text_hash, "text")
    text = _decode_utf8(text_bytes, field="text object")
    start, end = _span_bytes(evidence_value["byte_start"], evidence_value["byte_end"], text_bytes)
    excerpt_bytes = text_bytes[start:end]
    actual_excerpt_hash = hash_bytes(excerpt_bytes)
    if actual_excerpt_hash != evidence_value["excerpt_hash"]:
        _fail(
            "invalid-span",
            "evidence excerpt hash does not match retained text",
            details={"expected": evidence_value["excerpt_hash"], "actual": actual_excerpt_hash},
        )

    char_start = len(text_bytes[:start].decode("utf-8"))
    char_end = len(text_bytes[:end].decode("utf-8"))
    context_start = max(0, char_start - context_characters)
    context_end = min(len(text), char_end + context_characters)
    result: dict[str, Any] = {
        **evidence_value,
        "excerpt": excerpt_bytes.decode("utf-8"),
        "context": text[context_start:context_end],
        "context_start": context_start,
        "context_end": context_end,
        "processing": descriptor_value["processing"],
        "extraction": dict(descriptor_value["extraction"]),
        "complete": descriptor_value["extraction"]["completeness"] == "complete",
    }
    return result


def read_source_page(
    descriptor: Mapping[str, Any],
    read_object: Callable[[str], bytes],
    *,
    offset: int = 0,
    limit: int = 4000,
) -> dict[str, Any]:
    """Read a bounded Unicode-character page from retained extracted text."""

    descriptor_value = _validated_descriptor(descriptor)
    _require_reader(read_object)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        _fail("invalid-request", "offset must be a non-negative integer")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or limit > _MAX_PAGE_CHARACTERS
    ):
        _fail("invalid-request", "limit must be a positive reasonable integer")

    text_hash = descriptor_value.get("text_version")
    if not isinstance(text_hash, str):
        _fail("source-unavailable", "source has no retained extracted text")
    _read_verified(read_object, descriptor_value["original_hash"], "original")
    text_bytes = _read_verified(read_object, text_hash, "text")
    text = _decode_utf8(text_bytes, field="text object")
    total_characters = len(text)
    if offset > total_characters:
        _fail(
            "invalid-request",
            "offset is beyond the end of the retained text",
            details={"offset": offset, "total_characters": total_characters},
        )
    end = min(offset + limit, total_characters)
    next_offset = end if end < total_characters else None
    extraction_completeness = descriptor_value["extraction"]["completeness"]
    page_complete = extraction_completeness == "complete" and next_offset is None
    limitations: list[str] = []
    if extraction_completeness != "complete":
        limitations.append(f"extraction is {extraction_completeness}")
    if next_offset is not None:
        limitations.append("page is truncated; request next_offset to continue")
    return {
        "source_id": descriptor_value["id"],
        "source_version": descriptor_value["version"],
        "text_version": text_hash,
        "source_family_id": descriptor_value["source_family_id"],
        "text": text[offset:end],
        "offset": offset,
        "next_offset": next_offset,
        "total_characters": total_characters,
        "truncated": next_offset is not None,
        "end_of_source": next_offset is None,
        "complete": page_complete and offset == 0,
        "completeness": extraction_completeness,
        "processing": descriptor_value["processing"],
        "limitations": limitations,
    }


def read_original(
    descriptor: Mapping[str, Any], read_object: Callable[[str], bytes]
) -> bytes:
    """Read the immutable original object named by a validated descriptor.

    Delayed extraction uses this content-addressed seam instead of reopening a
    path, so an interrupted capture cannot accidentally process changed bytes.
    """

    descriptor_value = _validated_descriptor(descriptor)
    _require_reader(read_object)
    return _read_verified(read_object, descriptor_value["original_hash"], "original")


def _extraction(
    supplied: Mapping[str, Any] | None,
    *,
    text_supplied: bool,
    is_text_media: bool,
    explicit_text_differs: bool,
) -> dict[str, str]:
    if supplied is None:
        if text_supplied and is_text_media and not explicit_text_differs:
            return {"method": "utf8-preserve", "version": "1", "completeness": "complete"}
        if text_supplied:
            return {"method": "explicit", "version": "1", "completeness": "complete"}
        return {"method": "unavailable", "version": "1", "completeness": "failed"}
    if not isinstance(supplied, Mapping):
        _fail("invalid-request", "extraction must be an object")
    if set(supplied) != {"method", "version", "completeness"}:
        _fail("invalid-request", "extraction must contain exactly method, version and completeness")
    method = supplied["method"]
    version = supplied["version"]
    completeness = supplied["completeness"]
    _require_nonempty_string(method, "extraction.method")
    _require_nonempty_string(version, "extraction.version")
    if completeness not in _COMPLETENESS:
        _fail("invalid-request", "extraction.completeness is invalid")
    return {"method": method, "version": version, "completeness": completeness}


def _processing(
    extraction: Mapping[str, str], *, extraction_supplied: bool, explicit_text: bool
) -> str:
    completeness = extraction["completeness"]
    if completeness == "pending":
        return "captured"
    if completeness == "failed":
        return "failed" if extraction_supplied else "captured"
    if completeness == "partial":
        return "partly-processed"
    return "processed" if extraction_supplied or explicit_text else "captured"


def _validated_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(descriptor, Mapping):
        _fail("invalid-request", "descriptor must be an object")
    value = dict(descriptor)
    if "version" not in value or not isinstance(value["version"], str):
        _fail("invalid-request", "descriptor version is required")
    if value["version"] != version_for("source_version", value):
        _fail("invalid-request", "descriptor version does not match its content")
    _validate_descriptor(value)
    return value


def _validate_descriptor(descriptor: Mapping[str, Any]) -> None:
    try:
        validate_payload("source_version", dict(descriptor))
    except V2Error:
        raise
    except Exception as exc:
        _fail(
            "invalid-request",
            "source descriptor failed contract validation",
            details={"error": str(exc)},
        )
    if descriptor.get("processing") not in _PROCESSING:
        _fail("invalid-request", "source descriptor processing state is invalid")
    extraction = descriptor.get("extraction")
    if not isinstance(extraction, Mapping) or set(extraction) != {
        "method",
        "version",
        "completeness",
    }:
        _fail("invalid-request", "source descriptor extraction is invalid")
    if descriptor.get("text_version") is not None:
        _validate_hash(descriptor["text_version"], "text_version")
    _validate_hash(descriptor.get("original_hash"), "original_hash")
    _validate_hash(descriptor.get("version"), "version")


def _validated_evidence(
    evidence: Mapping[str, Any], descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        _fail("invalid-span", "evidence must be an object")
    value = dict(evidence)
    _validate_evidence_shape(value)
    fields = {
        "source_id": "id",
        "source_version": "version",
        "text_version": "text_version",
        "source_family_id": "source_family_id",
    }
    for evidence_key, descriptor_key in fields.items():
        if value[evidence_key] != descriptor.get(descriptor_key):
            _fail(
                "invalid-span",
                f"evidence {evidence_key} does not match descriptor",
                details={"field": evidence_key},
            )
    _validate_hash(value["source_version"], "source_version")
    _validate_hash(value["text_version"], "text_version")
    _validate_hash(value["excerpt_hash"], "excerpt_hash")
    return value


def _validate_evidence_shape(value: Mapping[str, Any]) -> None:
    required = {
        "source_id",
        "source_version",
        "text_version",
        "byte_start",
        "byte_end",
        "excerpt_hash",
        "source_family_id",
    }
    allowed = required | {"original_locator", "speaker"}
    if not required.issubset(value) or set(value) - allowed:
        _fail("invalid-span", "evidence is missing required references")
    if not isinstance(value["source_id"], str) or not value["source_id"]:
        _fail("invalid-span", "evidence source_id is invalid")
    if not isinstance(value["source_family_id"], str) or not value["source_family_id"]:
        _fail("invalid-span", "evidence source_family_id is invalid")
    if "original_locator" in value:
        _validate_locator(value["original_locator"])
    if "speaker" in value:
        _require_nonempty_string(value["speaker"], "speaker")


def _validate_locator(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"kind", "value"}:
        _fail("invalid-span", "evidence original_locator is invalid")
    if value["kind"] not in {"text", "pdf-page", "csv-row", "transcript-time", "other"}:
        _fail("invalid-span", "evidence original_locator kind is invalid")
    _require_nonempty_string(value["value"], "original_locator.value")


def _span(start: Any, end: Any, byte_length: int) -> tuple[int, int]:
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > byte_length
    ):
        _fail(
            "invalid-span",
            "evidence span must be non-empty and inside the retained text",
            details={"byte_start": start, "byte_end": end, "byte_length": byte_length},
        )
    return start, end


def _span_bytes(start: Any, end: Any, data: bytes) -> tuple[int, int]:
    start, end = _span(start, end, len(data))
    if (start < len(data) and data[start] & 0xC0 == 0x80) or (
        end < len(data) and data[end] & 0xC0 == 0x80
    ):
        _fail("invalid-span", "evidence span cuts through a UTF-8 code point")
    return start, end


def _require_reader(read_object: Any) -> None:
    if not callable(read_object):
        _fail("invalid-request", "read_object must be callable")


def _read_verified(read_object: Callable[[str], bytes], object_hash: str, label: str) -> bytes:
    _validate_hash(object_hash, label + " hash")
    try:
        data = read_object(object_hash)
    except V2Error:
        raise
    except Exception as exc:
        _fail(
            "source-unavailable",
            f"retained {label} object is unavailable",
            details={"hash": object_hash, "error": exc.__class__.__name__},
        )
    if not isinstance(data, bytes):
        _fail("source-unavailable", f"retained {label} object did not return bytes")
    actual_hash = hash_bytes(data)
    if actual_hash != object_hash:
        _fail(
            "source-unavailable",
            f"retained {label} object failed hash verification",
            details={"expected": object_hash, "actual": actual_hash},
        )
    return data


def _retain(data: bytes, objects: dict[str, bytes]) -> str:
    object_hash = hash_bytes(data)
    objects[object_hash] = data
    return object_hash


def _decode_utf8(data: bytes, *, field: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(
            "source-unavailable",
            f"{field} is not valid UTF-8",
            details={"reason": "invalid-utf8", "position": exc.start},
        )


def _source_id(value: str | None, field: str) -> str:
    if value is None:
        return generate_ulid()
    _require_nonempty_string(value, field)
    return value


def _capture_time(value: str | None) -> str:
    if value is None:
        return utc_now()
    _require_nonempty_string(value, "captured_at")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        _fail(
            "invalid-request",
            "captured_at must be an ISO-8601 date-time",
            details={"error": str(exc)},
        )
    if parsed.tzinfo is None:
        _fail("invalid-request", "captured_at must include a timezone")
    return value


def _require_bytes(value: Any, field: str) -> None:
    if not isinstance(value, bytes):
        _fail("invalid-request", f"{field} must be bytes")


def _require_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail("invalid-request", f"{field} must be a non-empty string")


def _validate_hash(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) != _HEX_HASH_LENGTH:
        _fail("invalid-request", f"{field} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError:
        _fail("invalid-request", f"{field} must be a SHA-256 hex digest")
    if value != value.casefold():
        _fail("invalid-request", f"{field} must use lowercase hexadecimal")


def _fail(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise V2Error(code, message, details=dict(details) if details is not None else None)
