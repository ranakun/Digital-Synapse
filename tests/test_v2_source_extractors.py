from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

import synapse.source_extractors as source_extractors
from synapse.source_extractors import (
    extract_retained_source,
    prepare_source_file,
    retain_source_file,
)
from synapse.source_store import evidence_ref, prepare_source
from synapse.v2_contracts import V2Error


def _assert_original_retained(raw: bytes, descriptor: dict, objects: dict[str, bytes]) -> None:
    assert objects[descriptor["original_hash"]] == raw


@pytest.mark.parametrize(
    ("suffix", "media_type", "raw"),
    [
        (".md", "text/markdown", "# Café 😀\n"),
        (".txt", "text/plain", "plain\ntext\u0301"),
        (".csv", "text/csv", "name,notes\nTaylor,café\n"),
        (".json", "application/json", '{"name":"Taylor","note":"café"}\n'),
        (".jsonl", "application/jsonl", '{"n":1}\n{"n":2}\n'),
        (".yaml", "application/yaml", "name: Taylor\nemoji: 😀\n"),
        (".xml", "application/xml", "<note>café</note>\n"),
    ],
)
def test_utf8_formats_preserve_exact_bytes_and_text(
    tmp_path: Path, suffix: str, media_type: str, raw: str
) -> None:
    path = tmp_path / f"source{suffix}"
    raw_bytes = raw.encode("utf-8")
    path.write_bytes(raw_bytes)

    descriptor, objects = prepare_source_file(path, origin="synthetic export")

    assert descriptor["media_type"] == media_type
    assert descriptor["extraction"] == {
        "method": "utf8-preserve",
        "version": "1",
        "completeness": "complete",
    }
    assert objects[descriptor["original_hash"]] == raw_bytes
    assert descriptor["text_version"] == descriptor["original_hash"]
    assert objects[descriptor["text_version"]] == raw_bytes


def test_empty_utf8_file_remains_retainable(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    descriptor, objects = prepare_source_file(path)

    assert descriptor["media_type"] == "text/plain"
    assert descriptor["text_version"] == descriptor["original_hash"]
    assert objects[descriptor["original_hash"]] == b""
    assert objects[descriptor["text_version"]] == b""
    assert descriptor["extraction"]["completeness"] == "complete"


@pytest.mark.parametrize("suffix", [".stderr", ".stdout", ".log"])
@pytest.mark.parametrize("raw", [b"", b"worker finished\n"])
def test_log_files_are_strict_utf8_plain_text(
    tmp_path: Path, suffix: str, raw: bytes
) -> None:
    path = tmp_path / f"run{suffix}"
    path.write_bytes(raw)

    descriptor, objects = prepare_source_file(path)

    assert descriptor["media_type"] == "text/plain"
    assert descriptor["extraction"] == {
        "method": "utf8-preserve",
        "version": "1",
        "completeness": "complete",
    }
    assert objects[descriptor["original_hash"]] == raw
    assert objects[descriptor["text_version"]] == raw


def test_invalid_utf8_log_is_failed_and_retained(tmp_path: Path) -> None:
    path = tmp_path / "run.stderr"
    raw = b"bad\xfflog"
    path.write_bytes(raw)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"] == {
        "method": "utf8-invalid",
        "version": "1",
        "completeness": "failed",
    }


def _write_pixmap_image(
    path: Path, *, colorspace: object, alpha: bool = False, width: int = 18, height: int = 12
) -> bytes:
    fitz = pytest.importorskip("fitz")
    pixmap = fitz.Pixmap(colorspace, fitz.IRect(0, 0, width, height), alpha)
    pixmap.save(str(path))
    return path.read_bytes()


def _configure_test_tessdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    model = b"deterministic synthetic English model"
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(model)
    monkeypatch.setenv("TESSDATA_PREFIX", str(tessdata))
    return tessdata, model


def _pdf_backend(fitz: object, text: str, seen: list[tuple[int, int, int]]) -> object:
    def backend(pixmap: object, tessdata: Path) -> bytes:
        seen.append((pixmap.width, pixmap.height, pixmap.alpha))
        assert tessdata.name == "tessdata"
        document = fitz.open()
        page = document.new_page(width=400, height=200)
        if text:
            page.insert_text((40, 80), text, fontsize=14)
        result = document.tobytes()
        document.close()
        return result

    return backend


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg"])
def test_image_ocr_retains_original_and_partial_text_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    fitz = pytest.importorskip("fitz")
    _tessdata, model = _configure_test_tessdata(tmp_path, monkeypatch)
    path = tmp_path / f"caption{suffix}"
    raw = _write_pixmap_image(path, colorspace=fitz.csRGB, width=800, height=600)
    seen: list[tuple[int, int, int]] = []
    monkeypatch.setattr(source_extractors, "_ocr_pdf", _pdf_backend(fitz, "I like the dark.", seen))

    descriptor, objects = prepare_source_file(
        path,
        source_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )

    expected_media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    assert descriptor["media_type"] == expected_media_type
    assert descriptor["extraction"]["method"] == "pymupdf-ocr"
    assert descriptor["extraction"]["completeness"] == "partial"
    assert descriptor["extraction"]["version"].startswith("pymupdf-")
    assert f"tesseract-eng-sha256-{hashlib.sha256(model).hexdigest()}" in descriptor[
        "extraction"
    ]["version"]
    assert "small3x" not in descriptor["extraction"]["version"]
    assert descriptor["text_version"] != descriptor["original_hash"]
    assert objects[descriptor["original_hash"]] == raw
    assert objects[descriptor["text_version"]] == b"I like the dark.\n"
    assert seen == [(800, 600, 0)]

    evidence = evidence_ref(
        descriptor,
        objects.__getitem__,
        0,
        len(objects[descriptor["text_version"]]),
    )
    assert evidence["source_version"] == descriptor["version"]
    assert evidence["text_version"] == descriptor["text_version"]


@pytest.mark.parametrize(
    ("suffix", "colorspace", "alpha"),
    [(".png", "gray", False), (".png", "rgb", True)],
)
def test_image_ocr_normalizes_grayscale_and_alpha_and_scales_small_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    colorspace: str,
    alpha: bool,
) -> None:
    fitz = pytest.importorskip("fitz")
    _configure_test_tessdata(tmp_path, monkeypatch)
    path = tmp_path / f"small{suffix}"
    source_colorspace = fitz.csGRAY if colorspace == "gray" else fitz.csRGB
    _write_pixmap_image(path, colorspace=source_colorspace, alpha=alpha)
    seen: list[tuple[int, int, int]] = []
    monkeypatch.setattr(source_extractors, "_ocr_pdf", _pdf_backend(fitz, "small caption", seen))

    descriptor, _objects = prepare_source_file(path)

    assert descriptor["extraction"]["completeness"] == "partial"
    assert "small3x" in descriptor["extraction"]["version"]
    assert seen == [(54, 36, 0)]


def test_image_ocr_no_text_is_a_specific_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fitz = pytest.importorskip("fitz")
    _configure_test_tessdata(tmp_path, monkeypatch)
    path = tmp_path / "blank.png"
    raw = _write_pixmap_image(path, colorspace=fitz.csRGB)
    monkeypatch.setattr(source_extractors, "_ocr_pdf", _pdf_backend(fitz, "", []))

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["method"] == "pymupdf-ocr-no-text"
    assert descriptor["extraction"]["completeness"] == "failed"
    assert "text_version" not in descriptor


def test_invalid_image_is_failed_without_losing_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_test_tessdata(tmp_path, monkeypatch)
    path = tmp_path / "damaged.png"
    raw = b"not an image"
    path.write_bytes(raw)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["method"].startswith("pymupdf-ocr-invalid-image-")
    assert descriptor["extraction"]["completeness"] == "failed"
    assert "text_version" not in descriptor


def test_image_ocr_reports_unavailable_model_without_stopping_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fitz = pytest.importorskip("fitz")
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "missing-tessdata"))
    path = tmp_path / "caption.png"
    raw = _write_pixmap_image(path, colorspace=fitz.csRGB)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["method"] == "pymupdf-ocr-model-unavailable-explicit"
    assert descriptor["extraction"]["completeness"] == "failed"
    assert "text_version" not in descriptor


def test_image_ocr_reports_unavailable_parser_without_stopping_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "caption.png"
    raw = b"synthetic image bytes"
    path.write_bytes(raw)
    monkeypatch.setitem(sys.modules, "fitz", None)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["method"].startswith("pymupdf-ocr-parser-unavailable-")
    assert descriptor["extraction"]["completeness"] == "failed"
    assert "text_version" not in descriptor


def test_pdf_uses_stream_bytes_and_keeps_late_page_qualification(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "brief.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=300)
    page.insert_text((40, 80), "Early statement", fontsize=12)
    document.new_page(width=400, height=300)
    late = document.new_page(width=400, height=300)
    late.insert_text((40, 80), "Qualification: only after the review date.", fontsize=12)
    document.save(str(path))
    document.close()
    raw = path.read_bytes()

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["media_type"] == "application/pdf"
    assert descriptor["extraction"]["completeness"] == "partial"
    text = objects[descriptor["text_version"]].decode("utf-8")
    assert "Early statement" in text
    assert "Qualification: only after the review date." in text
    assert "\f" in text


def test_docx_keeps_paragraph_and_table_body_order(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    path = tmp_path / "brief.docx"
    document = docx.Document()
    document.add_paragraph("Before the table")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Company"
    table.rows[0].cells[1].text = "Example Co"
    document.add_paragraph("After the table")
    document.save(path)
    raw = path.read_bytes()

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["media_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert descriptor["extraction"]["completeness"] == "complete"
    text = objects[descriptor["text_version"]].decode("utf-8")
    assert text.index("Before the table") < text.index("Company | Example Co") < text.index(
        "After the table"
    )


def test_unknown_binary_is_retained_with_failed_extraction(tmp_path: Path) -> None:
    path = tmp_path / "opaque.bin"
    raw = b"\x00\xff\x10opaque bytes"
    path.write_bytes(raw)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["completeness"] == "failed"
    assert descriptor["extraction"]["method"] == "binary-unsupported"
    assert "text_version" not in descriptor
    assert list(objects) == [descriptor["original_hash"]]


def test_damaged_optional_formats_are_failed_but_original_is_retained(tmp_path: Path) -> None:
    path = tmp_path / "damaged.pdf"
    raw = b"this is not a PDF"
    path.write_bytes(raw)

    descriptor, objects = prepare_source_file(path)

    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"]["completeness"] == "failed"
    assert descriptor["extraction"]["method"].startswith("pymupdf-failed-")
    assert "text_version" not in descriptor


def test_invalid_utf8_and_input_failures_are_explicit(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    raw = b"{\xff}"
    invalid.write_bytes(raw)
    descriptor, objects = prepare_source_file(invalid)
    _assert_original_retained(raw, descriptor, objects)
    assert descriptor["extraction"] == {
        "method": "utf8-invalid",
        "version": "1",
        "completeness": "failed",
    }

    with pytest.raises(V2Error) as raised:
        prepare_source_file(tmp_path / "missing.txt")
    assert raised.value.code == "source-unavailable"


def test_env_files_are_rejected_before_reading(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"TOKEN=synthetic")

    with pytest.raises(V2Error) as raised:
        prepare_source_file(path)
    assert raised.value.code == "invalid-request"
    assert raised.value.details["reason"] == "env-file-blocked"


def test_retain_source_file_is_pending_before_optional_extraction(tmp_path: Path) -> None:
    path = tmp_path / "brief.pdf"
    raw = b"%PDF-retained-before-parser"
    path.write_bytes(raw)

    descriptor, objects = retain_source_file(path, source_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert descriptor["extraction"]["completeness"] == "pending"
    assert descriptor["processing"] == "captured"
    assert descriptor["original_hash"] in objects
    assert objects[descriptor["original_hash"]] == raw


def test_extract_retained_source_uses_original_object_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "brief.pdf"
    raw = b"%PDF-retained-before-parser"
    path.write_bytes(raw)
    descriptor, retained = retain_source_file(
        path,
        source_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        source_family_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        captured_at="2026-09-10T08:00:00Z",
    )
    monkeypatch.setitem(sys.modules, "fitz", None)

    extracted, objects = extract_retained_source(descriptor, retained.__getitem__)

    assert extracted["id"] == descriptor["id"]
    assert extracted["source_family_id"] == descriptor["source_family_id"]
    assert extracted["captured_at"] == descriptor["captured_at"]
    assert extracted["original_hash"] == descriptor["original_hash"]
    assert extracted["extraction"]["completeness"] == "failed"
    assert objects[descriptor["original_hash"]] == raw


def test_extract_retained_source_accepts_an_already_complete_descriptor() -> None:
    descriptor, retained = prepare_source(
        b"already retained text",
        origin="complete retained source",
        source_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        source_family_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        captured_at="2026-09-10T08:00:00Z",
    )

    extracted, objects = extract_retained_source(descriptor, retained.__getitem__)

    assert extracted == descriptor
    assert objects == retained
