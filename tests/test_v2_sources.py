from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256

import pytest

from synapse.source_store import evidence_ref, prepare_source, read_passage, read_source_page
from synapse.v2_contracts import V2Error

SOURCE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
FAMILY_ID = "01BX5ZZKBKACTAV9WEVGEMMVRZ"


def _reader(objects: dict[str, bytes]) -> Callable[[str], bytes]:
    return objects.__getitem__


def _source(text: str = "prefix\nlate evidence — café 😀\nsuffix\n"):
    return prepare_source(
        text.encode("utf-8"),
        origin="synthetic fixture",
        source_id=SOURCE_ID,
        source_family_id=FAMILY_ID,
        captured_at="2026-09-10T08:00:00Z",
    )


def _error_code(callable_: Callable[[], object]) -> str:
    with pytest.raises(V2Error) as caught:
        callable_()
    return caught.value.code


def test_prepare_source_retains_exact_utf8_bytes_and_descriptor_hashes() -> None:
    raw = "first\r\nlate passage — café 😀\r\nlast".encode()
    descriptor, objects = prepare_source(
        raw,
        origin="export",
        source_id=SOURCE_ID,
        captured_at="2026-09-10T08:00:00+00:00",
    )

    assert descriptor["original_hash"] == sha256(raw).hexdigest()
    assert descriptor["text_version"] == descriptor["original_hash"]
    assert objects[descriptor["original_hash"]] == raw
    assert descriptor["extraction"] == {
        "method": "utf8-preserve",
        "version": "1",
        "completeness": "complete",
    }


def test_empty_utf8_source_retains_empty_text_and_zero_character_page() -> None:
    descriptor, objects = prepare_source(
        b"", origin="empty export", source_id=SOURCE_ID, captured_at="2026-09-10T08:00:00Z"
    )

    assert descriptor["original_hash"] == descriptor["text_version"]
    assert objects[descriptor["original_hash"]] == b""
    page = read_source_page(descriptor, _reader(objects))
    assert page["text"] == ""
    assert page["offset"] == 0
    assert page["next_offset"] is None
    assert page["total_characters"] == 0
    assert page["end_of_source"] is True
    assert page["complete"] is True


def test_explicit_text_media_replacement_is_marked_explicit() -> None:
    descriptor, _objects = prepare_source(
        b"original", origin="export", source_id=SOURCE_ID, text="revised"
    )

    assert descriptor["extraction"] == {
        "method": "explicit",
        "version": "1",
        "completeness": "complete",
    }


def test_binary_original_and_explicit_text_are_independent_objects() -> None:
    raw = b"%PDF-binary-original\x00"
    extracted = "page one\npage two — 😀"
    descriptor, objects = prepare_source(
        raw,
        origin="report.pdf",
        media_type="application/pdf",
        source_id=SOURCE_ID,
        text=extracted,
        extraction={"method": "fixture-pdf", "version": "1", "completeness": "partial"},
    )

    assert descriptor["original_hash"] != descriptor["text_version"]
    assert objects[descriptor["original_hash"]] == raw
    assert objects[descriptor["text_version"]] == extracted.encode("utf-8")
    assert descriptor["processing"] == "partly-processed"


def test_binary_without_extractor_preserves_original_without_fake_text() -> None:
    descriptor, objects = prepare_source(
        b"\x89PNG\r\n\x1a\n", origin="image", media_type="image/png", source_id=SOURCE_ID
    )

    assert "text_version" not in descriptor
    assert descriptor["extraction"]["completeness"] == "failed"
    assert descriptor["processing"] == "captured"
    assert list(objects) == [descriptor["original_hash"]]
    assert (
        _error_code(lambda: read_source_page(descriptor, _reader(objects))) == "source-unavailable"
    )


def test_pending_extraction_is_captured_without_fake_text() -> None:
    descriptor, objects = prepare_source(
        b"opaque retained bytes",
        origin="pending source",
        media_type="application/pdf",
        source_id=SOURCE_ID,
        extraction={"method": "pending", "version": "1", "completeness": "pending"},
    )

    assert descriptor["extraction"]["completeness"] == "pending"
    assert descriptor["processing"] == "captured"
    assert "text_version" not in descriptor
    assert objects[descriptor["original_hash"]] == b"opaque retained bytes"


def test_read_passage_verifies_late_unicode_span_and_context() -> None:
    descriptor, objects = _source()
    reader = _reader(objects)
    text = objects[descriptor["text_version"]]
    excerpt = "late evidence — café 😀".encode()
    start = text.index(excerpt)
    evidence = evidence_ref(descriptor, reader, start, start + len(excerpt), speaker="Taylor")

    result = read_passage(descriptor, evidence, reader, context_characters=7)

    assert result["excerpt"] == "late evidence — café 😀"
    assert result["context"] == "prefix\nlate evidence — café 😀\nsuffix"
    assert result["source_version"] == descriptor["version"]
    assert result["text_version"] == descriptor["text_version"]
    assert result["speaker"] == "Taylor"


def test_invalid_empty_or_mid_codepoint_spans_are_rejected() -> None:
    descriptor, objects = _source("a😀b")
    reader = _reader(objects)
    emoji_start = 1
    emoji_end = emoji_start + len("😀".encode())

    assert _error_code(lambda: evidence_ref(descriptor, reader, 0, 0)) == "invalid-span"
    assert (
        _error_code(lambda: evidence_ref(descriptor, reader, emoji_start + 1, emoji_end))
        == "invalid-span"
    )
    assert (
        _error_code(lambda: evidence_ref(descriptor, reader, emoji_start, emoji_end - 1))
        == "invalid-span"
    )


def test_changed_object_and_descriptor_or_evidence_references_fail() -> None:
    descriptor, objects = _source()
    reader = _reader(objects)
    evidence = evidence_ref(descriptor, reader, 0, len(b"prefix"))

    changed = dict(objects)
    changed[descriptor["text_version"]] = b"changed"
    assert (
        _error_code(lambda: read_passage(descriptor, evidence, _reader(changed)))
        == "source-unavailable"
    )

    changed_original = dict(objects)
    changed_original[descriptor["original_hash"]] = b"changed original"
    assert (
        _error_code(lambda: read_source_page(descriptor, _reader(changed_original)))
        == "source-unavailable"
    )

    changed_descriptor = dict(descriptor)
    changed_descriptor["origin"] = "renamed"
    assert _error_code(lambda: read_source_page(changed_descriptor, reader)) == "invalid-request"

    wrong_evidence = dict(evidence)
    wrong_evidence["source_family_id"] = SOURCE_ID
    assert _error_code(lambda: read_passage(descriptor, wrong_evidence, reader)) == "invalid-span"


def test_locator_and_speaker_survive_passage_read() -> None:
    descriptor, objects = _source("a cited line")
    reader = _reader(objects)
    evidence = evidence_ref(
        descriptor,
        reader,
        2,
        len(b"a cited line"),
        original_locator={"kind": "text", "value": "line 1"},
        speaker="Taylor",
    )

    result = read_passage(descriptor, evidence, reader)

    assert result["original_locator"] == {"kind": "text", "value": "line 1"}
    assert result["speaker"] == "Taylor"


def test_source_reading_is_independent_of_origin_name() -> None:
    descriptor, objects = _source("stable content")
    renamed = dict(descriptor)
    renamed["origin"] = "renamed/or/moved.txt"
    # A caller must not be able to alter the descriptor, even though paths are
    # not used to locate content.  The unchanged descriptor remains readable.
    assert read_source_page(descriptor, _reader(objects), limit=100)["text"] == "stable content"
    assert _error_code(lambda: read_source_page(renamed, _reader(objects))) == "invalid-request"


def test_source_page_pagination_reconstructs_exact_text() -> None:
    original = "header\n" + "late — 😀\n" * 800 + "trailer"
    descriptor, objects = _source(original)
    reader = _reader(objects)
    pieces: list[str] = []
    offset = 0
    while True:
        page = read_source_page(descriptor, reader, offset=offset, limit=37)
        pieces.append(page["text"])
        if page["next_offset"] is None:
            assert page["complete"] is False
            assert page["end_of_source"] is True
            break
        offset = page["next_offset"]

    assert "".join(pieces) == original
    assert page["total_characters"] == len(original)
    assert page["truncated"] is False


def test_page_after_source_start_reports_termination_without_full_completeness() -> None:
    descriptor, objects = _source("0123456789")

    page = read_source_page(descriptor, _reader(objects), offset=5, limit=5)

    assert page["text"] == "56789"
    assert page["next_offset"] is None
    assert page["end_of_source"] is True
    assert page["complete"] is False


def test_partial_extraction_never_reports_complete() -> None:
    descriptor, objects = prepare_source(
        b"binary",
        origin="partial extraction",
        media_type="application/octet-stream",
        source_id=SOURCE_ID,
        text="only the first page",
        extraction={"method": "fixture", "version": "1", "completeness": "partial"},
    )

    page = read_source_page(descriptor, _reader(objects), limit=4000)
    assert page["completeness"] == "partial"
    assert page["complete"] is False
    assert "partial" in " ".join(page["limitations"])


def test_invalid_utf8_original_is_rejected_for_text_media() -> None:
    assert (
        _error_code(lambda: prepare_source(b"valid\xff", origin="bad.txt", media_type="text/plain"))
        == "invalid-request"
    )


def test_page_limits_and_offsets_are_explicitly_validated() -> None:
    descriptor, objects = _source("short")
    reader = _reader(objects)
    assert _error_code(lambda: read_source_page(descriptor, reader, limit=0)) == "invalid-request"
    assert _error_code(lambda: read_source_page(descriptor, reader, offset=99)) == "invalid-request"
    assert (
        _error_code(lambda: read_source_page(descriptor, reader, limit=32_001)) == "invalid-request"
    )
