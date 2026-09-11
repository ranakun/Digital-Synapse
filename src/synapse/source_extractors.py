"""Read-only adapters from source files to retained v2 source objects.

The adapter owns format detection and extraction only.  It never writes the
input file or a vault, and it delegates descriptor/object construction to
``source_store.prepare_source``.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .source_store import prepare_source, read_original
from .v2_contracts import V2Error, hash_bytes, validate_payload

__all__ = ["extract_retained_source", "prepare_source_file", "retain_source_file"]


_EXTRACTION_VERSION = "1"
_OCR_LANGUAGE = "eng"
_OCR_MODEL = "eng.traineddata"
_UTF8_FORMATS: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "application/xml",
    ".stderr": "text/plain",
    ".stdout": "text/plain",
    ".log": "text/plain",
}
_BINARY_FORMATS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_IMAGE_FORMATS: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class _ImageOCRFailure(Exception):
    """Internal failure carrying a stable extraction method suffix."""

    def __init__(self, kind: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.kind = kind
        self.cause = cause


def prepare_source_file(
    path: Path,
    *,
    origin: str | None = None,
    source_id: str | None = None,
    source_family_id: str | None = None,
    captured_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Prepare one explicitly supplied file for retention.

    The exact input bytes are always passed to :func:`prepare_source`.  Known
    UTF-8 formats retain those bytes as their text version; PDF and DOCX use
    optional parsers over an in-memory byte stream.  JPEG/JPG/PNG files use
    local English OCR through optional PyMuPDF and Tesseract language data.
    OCR is a text recovery aid: it does not fully understand images or
    diagrams.  Unsupported or failed extraction retains only the original
    object and marks extraction failed.  The internal OCR helper is replaceable
    in tests; the production backend uses ``Pixmap.pdfocr_tobytes``.
    """

    try:
        source_path = Path(path)
    except (TypeError, ValueError) as exc:
        raise V2Error(
            "invalid-request",
            "path must identify one input file",
            details={"error": exc.__class__.__name__},
        ) from exc

    if _is_env_file(source_path):
        raise V2Error(
            "invalid-request",
            "refusing to read an environment file",
            details={"reason": "env-file-blocked"},
        )

    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise V2Error(
            "source-unavailable",
            "unable to read the supplied source file",
            details={"error": exc.__class__.__name__},
        ) from exc

    common = {
        "origin": str(source_path) if origin is None else origin,
        "source_id": source_id,
        "source_family_id": source_family_id,
        "captured_at": captured_at,
    }
    suffix = source_path.suffix.casefold()

    media_type = _UTF8_FORMATS.get(suffix)
    if media_type is not None:
        return _prepare_utf8(raw, media_type=media_type, common=common)

    if suffix == ".pdf":
        return _prepare_pdf(raw, common=common)
    if suffix == ".docx":
        return _prepare_docx(raw, common=common)
    image_media_type = _IMAGE_FORMATS.get(suffix)
    if image_media_type is not None:
        return _prepare_image(
            raw,
            media_type=image_media_type,
            common=common,
        )

    method = "binary-unsupported" if b"\x00" in raw else "unsupported-format"
    return _prepare_failed(
        raw,
        media_type="application/octet-stream",
        method=method,
        common=common,
    )


def retain_source_file(
    path: Path,
    *,
    origin: str | None = None,
    source_id: str | None = None,
    source_family_id: str | None = None,
    captured_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Retain one file before attempting any format-specific extraction.

    The input is read exactly once.  Recognized UTF-8 files become immediately
    searchable; all other inputs, including invalid UTF-8, receive a pending
    extraction descriptor and retain only their original bytes.  No parser or
    OCR backend is imported or invoked by this function.
    """

    source_path = _checked_path(path)
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise V2Error(
            "source-unavailable",
            "unable to read the supplied source file",
            details={"error": exc.__class__.__name__},
        ) from exc

    suffix = source_path.suffix.casefold()
    media_type = _UTF8_FORMATS.get(suffix)
    if media_type is None:
        media_type = _BINARY_FORMATS.get(suffix) or _IMAGE_FORMATS.get(suffix)
    if media_type is None:
        media_type = "application/octet-stream"
    common = _common(
        source_path,
        origin=origin,
        source_id=source_id,
        source_family_id=source_family_id,
        captured_at=captured_at,
    )
    if media_type in _UTF8_FORMATS.values():
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return prepare_source(
                raw,
                origin=common["origin"],
                media_type=media_type,
                source_id=common["source_id"],
                source_family_id=common["source_family_id"],
                captured_at=common["captured_at"],
                extraction={"method": "pending", "version": _EXTRACTION_VERSION, "completeness": "pending"},
            )
        return prepare_source(
            raw,
            origin=common["origin"],
            media_type=media_type,
            source_id=common["source_id"],
            source_family_id=common["source_family_id"],
            captured_at=common["captured_at"],
            text=None if media_type.startswith("text/") else decoded,
            extraction={"method": "utf8-preserve", "version": _EXTRACTION_VERSION, "completeness": "complete"},
        )
    return prepare_source(
        raw,
        origin=common["origin"],
        media_type=media_type,
        source_id=common["source_id"],
        source_family_id=common["source_family_id"],
        captured_at=common["captured_at"],
        extraction={"method": "pending", "version": _EXTRACTION_VERSION, "completeness": "pending"},
    )


def extract_retained_source(
    descriptor: Mapping[str, Any], read_object: Any
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Extract from the original object addressed by ``descriptor`` only."""

    validate_payload("source_version", dict(descriptor))
    raw = read_original(descriptor, read_object)
    objects = {descriptor["original_hash"]: raw}
    if descriptor.get("text_version"):
        text_hash = descriptor["text_version"]
        try:
            text = read_object(text_hash)
        except V2Error:
            raise
        except Exception as exc:
            raise V2Error(
                "source-unavailable",
                "retained extracted text is unavailable",
                details={"error": exc.__class__.__name__},
            ) from exc
        if not isinstance(text, bytes):
            raise V2Error("source-unavailable", "retained extracted text did not return bytes")
        if hash_bytes(text) != text_hash:
            raise V2Error("source-unavailable", "retained extracted text failed hash verification")
        objects[text_hash] = text
        return dict(descriptor), objects

    common = {
        "origin": descriptor["origin"],
        "source_id": descriptor["id"],
        "source_family_id": descriptor["source_family_id"],
        "captured_at": descriptor["captured_at"],
    }
    media_type = descriptor["media_type"]
    if media_type.startswith("text/"):
        return _prepare_utf8(raw, media_type=media_type, common=common)
    if media_type == "application/pdf":
        return _prepare_pdf(raw, common=common)
    if media_type == _BINARY_FORMATS[".docx"]:
        return _prepare_docx(raw, common=common)
    if media_type in _IMAGE_FORMATS.values():
        return _prepare_image(raw, media_type=media_type, common=common)
    # Structured UTF-8 formats were retained with no parser requirement.
    if media_type in _UTF8_FORMATS.values():
        return _prepare_utf8(raw, media_type=media_type, common=common)
    return _prepare_failed(
        raw,
        media_type=media_type,
        method="unsupported-format",
        common=common,
    )


def _prepare_utf8(
    raw: bytes,
    *,
    media_type: str,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Retain a recognized UTF-8 format without normalizing its text."""

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        # ``prepare_source`` validates text/* bytes before it can construct a
        # failed descriptor.  Use the binary MIME fallback for invalid UTF-8
        # text formats so the original remains retainable and the failure
        # stays explicit.  Structured media types can retain their proper MIME
        # while failed because source_store does not decode them implicitly.
        return _prepare_failed(
            raw,
            media_type=(
                "application/octet-stream"
                if media_type.startswith("text/")
                else media_type
            ),
            method="utf8-invalid",
            common=common,
        )

    # Text media can be derived by source_store directly from raw, which also
    # preserves an empty text file.  Structured media types need the decoded
    # value supplied explicitly because source_store only auto-derives text
    # for text/* media.
    text = None if media_type.startswith("text/") else raw.decode("utf-8")
    return prepare_source(
        raw,
        origin=common["origin"],
        media_type=media_type,
        source_id=common["source_id"],
        source_family_id=common["source_family_id"],
        captured_at=common["captured_at"],
        text=text,
        extraction={
            "method": "utf8-preserve",
            "version": _EXTRACTION_VERSION,
            "completeness": "complete",
        },
    )


def _checked_path(path: Path) -> Path:
    try:
        source_path = Path(path)
    except (TypeError, ValueError) as exc:
        raise V2Error(
            "invalid-request",
            "path must identify one input file",
            details={"error": exc.__class__.__name__},
        ) from exc
    if _is_env_file(source_path):
        raise V2Error(
            "invalid-request",
            "refusing to read an environment file",
            details={"reason": "env-file-blocked"},
        )
    return source_path


def _common(
    source_path: Path,
    *,
    origin: str | None,
    source_id: str | None,
    source_family_id: str | None,
    captured_at: str | None,
) -> dict[str, Any]:
    return {
        "origin": str(source_path) if origin is None else origin,
        "source_id": source_id,
        "source_family_id": source_family_id,
        "captured_at": captured_at,
    }


def _prepare_pdf(
    raw: bytes,
    *,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        return _prepare_failed(
            raw,
            media_type="application/pdf",
            method=f"pymupdf-unavailable-{exc.__class__.__name__}",
            common=common,
        )

    try:
        document = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        return _prepare_failed(
            raw,
            media_type="application/pdf",
            method=f"pymupdf-failed-{exc.__class__.__name__}",
            common=common,
        )

    try:
        page_texts: list[str] = []
        missing_pages = 0
        for page in document:
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            if not text.strip():
                missing_pages += 1
            page_texts.append(text.strip())
    finally:
        document.close()

    extracted = "\n\f\n".join(page_texts)
    if not page_texts or not any(page_texts):
        return _prepare_failed(
            raw,
            media_type="application/pdf",
            method="pymupdf-text-failed",
            common=common,
        )

    completeness = "partial" if missing_pages else "complete"
    method = "pymupdf-text-partial" if completeness == "partial" else "pymupdf-text"
    return _prepare_extracted(
        raw,
        media_type="application/pdf",
        text=extracted,
        method=method,
        completeness=completeness,
        common=common,
    )


def _prepare_image(
    raw: bytes,
    *,
    media_type: str,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Retain an image and optionally recover bounded English OCR text."""

    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        return _prepare_failed(
            raw,
            media_type=media_type,
            method=f"pymupdf-ocr-parser-unavailable-{exc.__class__.__name__}",
            common=common,
        )

    tessdata_info = _resolve_tessdata(fitz)
    if tessdata_info is None:
        method = (
            "pymupdf-ocr-model-unavailable-explicit"
            if "TESSDATA_PREFIX" in os.environ
            else "pymupdf-ocr-model-unavailable"
        )
        return _prepare_failed(raw, media_type=media_type, method=method, common=common)

    tessdata, model_hash = tessdata_info
    parser_version = _pymupdf_version(fitz)
    try:
        text, scaled = _run_image_ocr(
            fitz,
            raw,
            tessdata=tessdata,
        )
    except _ImageOCRFailure as exc:
        method = f"pymupdf-ocr-{exc.kind}-{exc.cause.__class__.__name__}"
        return _prepare_failed(
            raw,
            media_type=media_type,
            method=method,
            extraction_version=_ocr_version(parser_version, model_hash, scaled=False),
            common=common,
        )

    extraction_version = _ocr_version(parser_version, model_hash, scaled=scaled)
    if not text.strip():
        return _prepare_failed(
            raw,
            media_type=media_type,
            method="pymupdf-ocr-no-text",
            extraction_version=extraction_version,
            common=common,
        )
    return _prepare_extracted(
        raw,
        media_type=media_type,
        text=text,
        method="pymupdf-ocr",
        completeness="partial",
        extraction_version=extraction_version,
        common=common,
    )


def _run_image_ocr(
    fitz: Any,
    raw: bytes,
    *,
    tessdata: Path,
) -> tuple[str, bool]:
    try:
        pixmap = fitz.Pixmap(raw)
    except Exception as exc:
        raise _ImageOCRFailure("invalid-image", exc) from exc

    try:
        pixmap = _rgb_without_alpha(fitz, pixmap)
        scaled = max(pixmap.width, pixmap.height) < 600
        if scaled:
            pixmap = fitz.Pixmap(pixmap, pixmap.width * 3, pixmap.height * 3)
        ocr_pdf = _ocr_pdf(pixmap, tessdata)
        if not isinstance(ocr_pdf, (bytes, bytearray, memoryview)):
            raise TypeError("OCR backend must return PDF bytes")
        document = fitz.open(stream=bytes(ocr_pdf), filetype="pdf")
        try:
            text = document[0].get_text("text") if len(document) else ""
        finally:
            document.close()
    except _ImageOCRFailure:
        raise
    except Exception as exc:
        raise _ImageOCRFailure("failed", exc) from exc
    return text, scaled


def _ocr_pdf(pixmap: Any, tessdata: Path) -> bytes:
    """Return PyMuPDF's searchable PDF representation for one image."""

    return pixmap.pdfocr_tobytes(
        language=_OCR_LANGUAGE,
        tessdata=str(tessdata),
    )


def _rgb_without_alpha(fitz: Any, pixmap: Any) -> Any:
    colorspace = getattr(pixmap, "colorspace", None)
    rgb = getattr(fitz, "csRGB", None)
    needs_conversion = getattr(colorspace, "n", None) != 3
    if needs_conversion:
        pixmap = fitz.Pixmap(rgb, pixmap)
    if getattr(pixmap, "alpha", False):
        pixmap = fitz.Pixmap(pixmap, 0)
    return pixmap


def _resolve_tessdata(fitz: Any) -> tuple[Path, str] | None:
    """Find the local English model without downloading or using global state."""

    if "TESSDATA_PREFIX" in os.environ:
        return _tessdata_info(Path(os.environ["TESSDATA_PREFIX"]))

    bundled = Path(sys.prefix) / "share" / "tessdata"
    info = _tessdata_info(bundled)
    if info is not None:
        return info

    try:
        helper_path = fitz.get_tessdata()
    except Exception:
        return None
    if not helper_path:
        return None
    return _tessdata_info(Path(helper_path))


def _tessdata_info(directory: Path) -> tuple[Path, str] | None:
    try:
        if directory.name == _OCR_MODEL and directory.is_file():
            model_path = directory
        else:
            if not directory.is_dir():
                return None
            model_path = directory / _OCR_MODEL
        model_bytes = model_path.read_bytes()
    except (OSError, ValueError):
        return None
    if not model_bytes:
        return None
    return model_path.parent, hashlib.sha256(model_bytes).hexdigest()


def _pymupdf_version(fitz: Any) -> str:
    value = getattr(fitz, "version", None)
    if isinstance(value, tuple) and value:
        value = value[0]
    if value is None:
        value = getattr(fitz, "VersionBind", "unknown")
    return str(value)


def _ocr_version(parser_version: str, model_hash: str, *, scaled: bool) -> str:
    suffix = ";small3x" if scaled else ""
    return f"pymupdf-{parser_version};tesseract-{_OCR_LANGUAGE}-sha256-{model_hash}{suffix}"


def _prepare_docx(
    raw: bytes,
    *,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    media_type = _BINARY_FORMATS[".docx"]
    try:
        import docx  # type: ignore[import-not-found]
        from docx.oxml.ns import qn  # type: ignore[import-not-found]
        from docx.table import Table  # type: ignore[import-not-found]
        from docx.text.paragraph import Paragraph  # type: ignore[import-not-found]
    except Exception as exc:
        return _prepare_failed(
            raw,
            media_type=media_type,
            method=f"python-docx-unavailable-{exc.__class__.__name__}",
            common=common,
        )

    try:
        document = docx.Document(io.BytesIO(raw))
        sections: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                value = Paragraph(child, document).text.strip()
            elif child.tag == qn("w:tbl"):
                value = _table_text(Table(child, document))
            else:
                continue
            if value:
                sections.append(value)
        unsupported_embedded = _has_unsupported_embedded_content(document)
    except Exception as exc:
        return _prepare_failed(
            raw,
            media_type=media_type,
            method=f"python-docx-failed-{exc.__class__.__name__}",
            common=common,
        )

    if not sections:
        return _prepare_failed(
            raw,
            media_type=media_type,
            method="python-docx-text-failed",
            common=common,
        )

    completeness = "partial" if unsupported_embedded else "complete"
    method = "python-docx-text-partial" if unsupported_embedded else "python-docx-text"
    return _prepare_extracted(
        raw,
        media_type=media_type,
        text="\n\n".join(sections),
        method=method,
        completeness=completeness,
        common=common,
    )


def _prepare_extracted(
    raw: bytes,
    *,
    media_type: str,
    text: str,
    method: str,
    completeness: str,
    extraction_version: str = _EXTRACTION_VERSION,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    return prepare_source(
        raw,
        origin=common["origin"],
        media_type=media_type,
        source_id=common["source_id"],
        source_family_id=common["source_family_id"],
        captured_at=common["captured_at"],
        text=text,
        extraction={
            "method": method,
            "version": extraction_version,
            "completeness": completeness,
        },
    )


def _prepare_failed(
    raw: bytes,
    *,
    media_type: str,
    method: str,
    extraction_version: str = _EXTRACTION_VERSION,
    common: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    return prepare_source(
        raw,
        origin=common["origin"],
        media_type=media_type,
        source_id=common["source_id"],
        source_family_id=common["source_family_id"],
        captured_at=common["captured_at"],
        extraction={
            "method": method,
            "version": extraction_version,
            "completeness": "failed",
        },
    )


def _table_text(table: Any) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _has_unsupported_embedded_content(document: Any) -> bool:
    if getattr(document, "inline_shapes", ()):
        return True
    unsupported_names = {"altChunk", "drawing", "embedded", "imagedata", "object", "oleObject"}
    for element in document.element.body.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in unsupported_names:
            return True
    return False


def _is_env_file(path: Path) -> bool:
    name = path.name.casefold()
    return name == ".env" or name.startswith(".env.")
