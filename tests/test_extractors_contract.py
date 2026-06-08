from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.extractors import extract_text


def test_text_markdown_csv_json_whatsapp_and_vcard_extractors(tmp_path: Path) -> None:
    txt = tmp_path / "note.txt"
    txt.write_text("Plain note about Example Person.", encoding="utf-8")
    assert "Example Person" in extract_text(txt).text

    md = tmp_path / "note.md"
    md.write_text("---\ntitle: ignored\n---\n\n# Meeting\nExample Company", encoding="utf-8")
    extracted_md = extract_text(md)
    assert extracted_md.format_hint == "markdown"
    assert "Example Company" in extracted_md.text
    assert "title:" not in extracted_md.text

    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text("name,email\nExample Person,person@example.test\n", encoding="utf-8")
    extracted_csv = extract_text(csv_path)
    assert extracted_csv.format_hint == "csv"
    assert "COLUMNS: name | email" in extracted_csv.text
    assert "Example Person" in extracted_csv.text

    json_path = tmp_path / "profile.json"
    json_path.write_text(
        json.dumps({"firstName": "Example", "lastName": "Person", "positions": []}),
        encoding="utf-8",
    )
    extracted_json = extract_text(json_path)
    assert extracted_json.format_hint == "json"
    assert extracted_json.metadata["source_shape"] == "linkedin"
    assert "Example" in extracted_json.text

    chat = tmp_path / "whatsapp-chat.txt"
    chat.write_text(
        "2026-06-08 09:30 - Example Person: Shipping the graph viewer today.\n"
        "2026-06-08 09:32 - Ops Lead: I will review the diff.\n",
        encoding="utf-8",
    )
    extracted_chat = extract_text(chat)
    assert extracted_chat.format_hint == "whatsapp-chat"
    assert extracted_chat.metadata["message_count"] == 2
    assert extracted_chat.metadata["participants"] == ["Example Person", "Ops Lead"]

    vcard = tmp_path / "contact.vcf"
    vcard.write_text(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Example Person\n"
        "EMAIL:person@example.test\nORG:Example Company\nEND:VCARD\n",
        encoding="utf-8",
    )
    extracted_vcard = extract_text(vcard)
    assert extracted_vcard.format_hint == "vcard"
    assert "FN: Example Person" in extracted_vcard.text
    assert "ORG: Example Company" in extracted_vcard.text


def test_docx_extractor_reads_paragraphs_and_tables(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    path = tmp_path / "brief.docx"
    document = docx.Document()
    document.add_paragraph("Example Person brief")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Company"
    table.rows[0].cells[1].text = "Example Company"
    document.save(path)

    extracted = extract_text(path)

    assert extracted.format_hint == "docx"
    assert "Example Person brief" in extracted.text
    assert "Company | Example Company" in extracted.text
    assert extracted.metadata["table_count"] == 1


def test_pdf_extractor_reads_text_pdf(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "brief.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 80), "Example Person PDF brief", fontsize=12)
    doc.save(str(path))
    doc.close()

    extracted = extract_text(path)

    assert extracted.format_hint == "pdf"
    assert "Example Person PDF brief" in extracted.text
    assert extracted.metadata["page_count"] == 1
