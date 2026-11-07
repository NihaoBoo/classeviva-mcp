from __future__ import annotations

import io
import zipfile

import openpyxl
import pymupdf
import pytest

import classeviva_mcp.pdf_utils as pdf_utils
from classeviva_mcp.pdf_utils import (
    extract_attachment_text,
    extract_docx_text,
    extract_pdf_text,
    extract_xlsx_text,
)


def _make_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Pagina {index + 1}")
    return doc.tobytes()


def _make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document)
    return buffer.getvalue()


def _make_xlsx(rows: int) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in range(1, rows + 1):
        ws.cell(row=row, column=1, value=f"riga {row}")
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_oversize_pdf_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_utils, "MAX_FILE_BYTES", 16)
    text, pages, included = extract_pdf_text(b"%PDF-1.4" + b"x" * 32)
    assert (text, pages, included) == ("", 0, [])


def test_pdf_page_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_utils, "MAX_PDF_PAGES", 3)
    pdf_bytes = _make_pdf(8)
    text, total_pages, included = extract_pdf_text(pdf_bytes)
    assert total_pages == 8
    assert len(included) <= 3
    assert "Limite di 3 pagine raggiunto su 8" in text


def test_docx_zip_bomb_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    docx_bytes = _make_docx(["contenuto abbastanza lungo"])
    monkeypatch.setattr(pdf_utils, "MAX_ZIP_UNCOMPRESSED_BYTES", 4)
    assert extract_docx_text(docx_bytes) == ""


def test_docx_xml_size_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    docx_bytes = _make_docx(["contenuto"])
    monkeypatch.setattr(pdf_utils, "MAX_XML_BYTES", 4)
    assert extract_docx_text(docx_bytes) == ""


def test_xlsx_row_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    xlsx_bytes = _make_xlsx(30)
    monkeypatch.setattr(pdf_utils, "MAX_XLSX_ROWS", 5)
    text = extract_xlsx_text(xlsx_bytes)
    assert "riga 1" in text
    assert "riga 5" in text
    assert "riga 30" not in text


def test_oversize_attachment_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_utils, "MAX_FILE_BYTES", 8)
    result = extract_attachment_text(b"x" * 32, file_name="allegato.pdf")
    assert "supera il limite" in result


def test_generic_zip_bomb_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("notes.txt", "contenuto " * 100)
    monkeypatch.setattr(pdf_utils, "MAX_ZIP_UNCOMPRESSED_BYTES", 4)
    result = extract_attachment_text(buffer.getvalue(), file_name="archivio.zip")
    assert "oltre il limite decompresso" in result


def test_zip_with_text_files_is_extracted() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("primo.txt", "ciao dal primo file")
    result = extract_attachment_text(buffer.getvalue(), file_name="archivio.zip")
    assert "ciao dal primo file" in result
