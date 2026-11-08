from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf

ROMAN_BY_DIGIT = {
    "1": "I",
    "2": "II",
    "3": "III",
    "4": "IV",
    "5": "V",
}

MAX_ZIP_FILES = 15
MAX_ZIP_UNCOMPRESSED_BYTES = 25 * 1024 * 1024  # 25 MB guard against zip bombs
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB per attachment
MAX_XML_BYTES = 10 * 1024 * 1024  # 10 MB for word/document.xml
MAX_PDF_PAGES = 50
MAX_XLSX_ROWS = 500
MAX_XLSX_COLS = 50
MAX_XLSX_SHEETS = 20
MAX_ZIP_DEPTH = 1

# Header of a class grid inside a timetable PDF, e.g. "5 AI - Settimana 8, Settimana 30".
CLASS_HEADER_RE = re.compile(r"\b([1-5])\s*([A-Z]{1,4})\s*-\s*Settimana", re.IGNORECASE)


def _page_class_regions(page: Any) -> list[tuple[float, float, str]]:
    """Finds the class grids laid out on a timetable page.

    A single PDF page often stacks several class timetables (one per class),
    each introduced by a header like ``5 AI - Settimana 8, Settimana 30``.
    Returns ``(y_top, y_bottom, class_code)`` for every grid found, ordered
    top to bottom.

    Returns an empty list when the page has fewer than two distinct headers:
    with a single class the whole page already is that class's timetable, and
    cutting by region would risk dropping legitimate circular text.
    """
    headers: list[tuple[float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            line_text = "".join(span.get("text", "") for span in line.get("spans", []))
            match = CLASS_HEADER_RE.search(line_text)
            if match:
                code = f"{match.group(1)}{match.group(2).upper()}"
                headers.append((float(line["bbox"][1]), code))
    if not headers:
        return []

    # Headers are often duplicated on the same baseline: collapse them.
    unique: dict[float, str] = {}
    for y_top, code in headers:
        unique.setdefault(round(y_top, 1), code)
    if len(unique) < 2:
        return []

    ordered = sorted(unique.items())
    page_height = float(page.rect.height)
    regions: list[tuple[float, float, str]] = []
    for index, (y_top, code) in enumerate(ordered):
        y_bottom = ordered[index + 1][0] if index + 1 < len(ordered) else page_height
        regions.append((y_top, y_bottom, code))
    return regions


def _text_in_regions(page: Any, regions: list[tuple[float, float]]) -> str:
    """Returns the text of the page restricted to the given vertical regions."""
    collected: list[tuple[float, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            y_top = float(line["bbox"][1])
            y_bottom = float(line["bbox"][3])
            line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not line_text:
                continue
            for region_top, region_bottom in regions:
                if y_top >= region_top - 1 and y_bottom <= region_bottom + 1:
                    collected.append((y_top, float(line["bbox"][0]), line_text))
                    break
    collected.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(text for _, _, text in collected)



def build_class_regex(filter_query: str) -> re.Pattern[str] | None:
    """Builds a tolerant case-insensitive regex from one or more AI-provided search terms
    (comma- or pipe-separated, e.g. '5AI, 5A INF, Informatica').
    Also tolerates spaces/ordinal symbols between year digit (1-5) and section letters (e.g. '5 AI', '5^AI', '5ª AI').
    """
    raw_terms = [t.strip() for t in re.split(r"[,|]", filter_query or "") if t.strip()]
    if not raw_terms:
        return None

    patterns: list[str] = []
    for term in raw_terms:
        compact = re.sub(r"\s+", "", term)
        m = re.match(r"^([1-5])([A-Za-z]{1,4})$", compact)
        if m:
            digit, letters = m.group(1), m.group(2)
            roman = ROMAN_BY_DIGIT.get(digit, digit)
            spaced_letters = r"\s*".join(re.escape(ch) for ch in letters)
            patterns.append(rf"\b(?:{digit}|{roman})\s*(?:[\^ª°\.]|a)?\s*{spaced_letters}\b")
        else:
            patterns.append(re.escape(term))

    return re.compile("|".join(patterns), re.IGNORECASE)


def _zip_total_uncompressed(zf: zipfile.ZipFile) -> int:
    return sum(info.file_size for info in zf.infolist() if not info.is_dir())


def extract_pdf_text(
    pdf_bytes: bytes,
    filter_keyword: str = "",
    max_chars: int = 14000,
) -> tuple[str, int, list[int]]:
    """Extracts selectable text from PDF bytes using PyMuPDF.

    When `filter_keyword` (e.g. '5AI') is provided and the PDF is a class
    timetable, the extraction is narrowed down to **the grid of that class
    only**: timetable pages usually stack several classes one after the other,
    so filtering by page alone would return the other classes as well.

    Returns: (extracted_text, total_pages, included_page_numbers_1based)
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return "", 0, []
    if len(pdf_bytes) > MAX_FILE_BYTES:
        return "", 0, []

    rx = build_class_regex(filter_keyword) if filter_keyword.strip() else None
    all_pages: list[tuple[int, str]] = []
    region_pages: list[tuple[int, str]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_pages = len(doc)
        for idx, page in enumerate(doc):
            if idx >= MAX_PDF_PAGES:
                break
            all_pages.append((idx + 1, page.get_text("text").strip()))
            if rx is not None:
                regions = _page_class_regions(page)
                if regions:
                    wanted = [
                        (top, bottom)
                        for top, bottom, code in regions
                        if rx.search(code) or rx.search(code.replace(" ", ""))
                    ]
                    if wanted:
                        region_pages.append((idx + 1, _text_in_regions(page, wanted)))

    if rx is not None and region_pages:
        # Class grids found: keep only the requested class.
        selected = region_pages
    elif rx is not None and total_pages > 2:
        # No recognisable class headers: fall back to page-level filtering.
        matched = [(pno, txt) for pno, txt in all_pages if pno == 1 or rx.search(txt)]
        selected = matched if any(rx.search(txt) for _, txt in matched) else all_pages
    else:
        selected = all_pages

    included_nums = [pno for pno, _ in selected]
    formatted = [f"[Pagina {pno}/{total_pages}]\n{txt}" for pno, txt in selected if txt]
    if total_pages > MAX_PDF_PAGES:
        formatted.append(f"[Limite di {MAX_PDF_PAGES} pagine raggiunto su {total_pages}]")
    joined = "\n\n".join(formatted).strip()
    if max_chars > 0 and len(joined) > max_chars:
        joined = joined[:max_chars] + f"\n... [Troncato a {max_chars} caratteri per risparmiare token]"
    return joined, total_pages, included_nums


def render_scanned_pdf_pages(
    pdf_bytes: bytes,
    notice_id: str,
    output_dir: Path,
    max_pages: int = 4,
    dpi: int = 150,
) -> list[str]:
    """Renders pages of a scanned (image-only) PDF to PNG files on disk at 150 DPI
    so multimodal MCP agents (Claude/Cursor/Antigravity) can inspect them via Vision.
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return []
    if len(pdf_bytes) > MAX_FILE_BYTES:
        return []
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_id = "".join(c for c in str(notice_id) if c.isalnum() or c in ("-", "_"))
    saved_images: list[str] = []

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for idx in range(min(len(doc), max_pages)):
            pix = doc[idx].get_pixmap(dpi=dpi)
            img_path = output_dir / f"circolare_{safe_id}_page_{idx + 1}.png"
            pix.save(str(img_path))
            saved_images.append(str(img_path))

    _prune_attachments_dir(output_dir)
    return saved_images


def extract_docx_text(docx_bytes: bytes, filter_keyword: str = "") -> str:
    """Extracts text from a Word .docx attachment using Python stdlib."""
    if not docx_bytes or not docx_bytes.startswith(b"PK"):
        return ""
    if len(docx_bytes) > MAX_FILE_BYTES:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            if "word/document.xml" not in zf.namelist():
                return ""
            if _zip_total_uncompressed(zf) > MAX_ZIP_UNCOMPRESSED_BYTES:
                return ""
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                return ""
            xml_content = zf.read("word/document.xml")
        root = ET.fromstring(xml_content)
        paragraphs: list[str] = []
        for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            texts = [t.text for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if t.text]
            if texts:
                paragraphs.append("".join(texts))
        full_text = "\n".join(paragraphs).strip()
        rx = build_class_regex(filter_keyword) if filter_keyword.strip() else None
        if rx and len(paragraphs) > 30:
            filtered = [paragraphs[0]] + [line for line in paragraphs[1:] if rx.search(line)]
            if len(filtered) > 1:
                return "\n".join(filtered)
        return full_text
    except Exception:
        return ""


def extract_xlsx_text(xlsx_bytes: bytes, filter_keyword: str = "") -> str:
    """Extracts timetable / tabular data from Excel (.xlsx) using openpyxl:
    - Unmerges all merged cells and propagates the top-left value into every sub-cell
      (critical for multi-hour school labs in merged cells).
    - If `filter_keyword` (e.g. '5AI') matches a column header, extracts vertically
      (Column A/B time slots + the matched class column).
    """
    if not xlsx_bytes or not xlsx_bytes.startswith(b"PK"):
        return ""
    if len(xlsx_bytes) > MAX_FILE_BYTES:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
            if _zip_total_uncompressed(zf) > MAX_ZIP_UNCOMPRESSED_BYTES:
                return ""
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    except Exception:
        return ""

    rx = build_class_regex(filter_keyword) if filter_keyword.strip() else None
    sheets_output: list[str] = []

    for sheet_name in wb.sheetnames[:MAX_XLSX_SHEETS]:
        ws = wb[sheet_name]
        # 1. Unmerge cells and fill every cell in the range with the top-left value
        merged_ranges = list(ws.merged_cells.ranges)[:100]
        for m_range in merged_ranges:
            if m_range.max_row > MAX_XLSX_ROWS or m_range.max_col > MAX_XLSX_COLS:
                continue
            top_val = ws.cell(m_range.min_row, m_range.min_col).value
            ws.unmerge_cells(str(m_range))
            if top_val is not None:
                for row_idx in range(m_range.min_row, m_range.max_row + 1):
                    for col_idx in range(m_range.min_col, m_range.max_col + 1):
                        ws.cell(row_idx, col_idx).value = top_val

        rows: list[list[str]] = []
        for row in ws.iter_rows(
            min_row=1,
            max_row=MAX_XLSX_ROWS,
            max_col=MAX_XLSX_COLS,
            values_only=True,
        ):
            str_row = [str(cell).strip() if cell is not None else "" for cell in row]
            if any(str_row):
                rows.append(str_row)

        if not rows:
            continue

        # 2. Check if `rx` matches a column in the first 5 header rows (vertical timetable layout)
        matched_cols: set[int] = set()
        if rx is not None:
            for r_idx in range(min(5, len(rows))):
                for c_idx, val in enumerate(rows[r_idx]):
                    if val and rx.search(val):
                        matched_cols.add(c_idx)

        if matched_cols:
            # Always keep leading context columns (e.g. Day / Hour in col 0 and 1) + matched class columns
            keep_cols = sorted({0, 1} | matched_cols)
            col_lines: list[str] = [f"[Foglio Excel: {sheet_name} — Estrazione Verticale Colonna {filter_keyword}]"]
            for row in rows:
                picked = [row[c] for c in keep_cols if c < len(row)]
                if any(picked):
                    col_lines.append(" | ".join(picked))
            sheets_output.append("\n".join(col_lines))
            continue

        # 3. Check if `rx` matches specific rows (horizontal layout)
        if rx is not None and len(rows) > 15:
            matching_rows = [rows[0]] + [r for r in rows[1:] if any(rx.search(c) for c in r)]
            if len(matching_rows) > 1:
                row_lines = [f"[Foglio Excel: {sheet_name} — Righe filtrate per {filter_keyword}]"]
                row_lines.extend(" | ".join(c for c in r if c) for r in matching_rows)
                sheets_output.append("\n".join(row_lines))
                continue

        # 4. Full sheet fallback
        all_lines = [f"[Foglio Excel: {sheet_name}]"]
        for row in rows[:120]:
            all_lines.append(" | ".join(c for c in row if c))
        sheets_output.append("\n".join(all_lines))

    return "\n\n".join(sheets_output).strip()


def extract_attachment_text(
    raw_bytes: bytes,
    file_name: str = "",
    filter_keyword: str = "",
    *,
    depth: int = 0,
) -> str:
    """Auto-detects PDF, DOCX, XLSX, CSV/TXT, or ZIP (with zip-bomb protection) and extracts text."""
    if not raw_bytes:
        return ""
    if len(raw_bytes) > MAX_FILE_BYTES:
        return f"[Allegato non elaborato: supera il limite di {MAX_FILE_BYTES // (1024 * 1024)} MB]"
    fname_low = (file_name or "").lower()

    if raw_bytes.startswith(b"%PDF") or fname_low.endswith(".pdf"):
        txt, _, _ = extract_pdf_text(raw_bytes, filter_keyword=filter_keyword)
        return txt

    if raw_bytes.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = zf.namelist()
                if _zip_total_uncompressed(zf) > MAX_ZIP_UNCOMPRESSED_BYTES:
                    return "[Allegato non elaborato: archivio ZIP oltre il limite decompresso]"
                if "word/document.xml" in names or fname_low.endswith(".docx"):
                    return extract_docx_text(raw_bytes, filter_keyword=filter_keyword)
                if any(n.startswith("xl/") for n in names) or fname_low.endswith(".xlsx"):
                    return extract_xlsx_text(raw_bytes, filter_keyword=filter_keyword)

                if depth >= MAX_ZIP_DEPTH:
                    return "[Archivio ZIP annidato ignorato per sicurezza]"

                # Generic .zip archive with zip-bomb guard
                extracted_chunks: list[str] = []
                total_uncompressed = 0
                for info in zf.infolist()[:MAX_ZIP_FILES]:
                    if info.is_dir() or info.filename.lower().endswith(".zip"):
                        continue
                    total_uncompressed += info.file_size
                    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                        extracted_chunks.append("[Avviso: Limite 25MB archivio ZIP raggiunto]")
                        break
                    if info.file_size > MAX_FILE_BYTES:
                        extracted_chunks.append(f"[File ZIP ignorato: {info.filename} troppo grande]")
                        continue
                    sub_bytes = zf.read(info.filename)
                    sub_txt = extract_attachment_text(
                        sub_bytes,
                        file_name=info.filename,
                        filter_keyword=filter_keyword,
                        depth=depth + 1,
                    )
                    if sub_txt:
                        extracted_chunks.append(f"=== File in ZIP: {info.filename} ===\n{sub_txt}")
                return "\n\n".join(extracted_chunks).strip()
        except Exception:
            return ""

    if fname_low.endswith((".csv", ".txt", ".md")):
        return raw_bytes.decode("utf-8", errors="replace")[:12000].strip()

    return ""


def _prune_attachments_dir(output_dir: Path, max_files: int = 30) -> None:
    """Mantiene al massimo gli ultimi `max_files` allegati/PNG nella cartella cache per evitare accumulo su disco."""
    try:
        files = [f for f in output_dir.iterdir() if f.is_file()]
        if len(files) > max_files:
            files.sort(key=lambda f: f.stat().st_mtime)
            for old_f in files[: len(files) - max_files]:
                old_f.unlink(missing_ok=True)
    except Exception:
        pass


def _write_private_bytes(target: Path, raw_bytes: bytes) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw_bytes)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def save_attachment_file(
    raw_bytes: bytes,
    notice_id: str,
    output_dir: Path,
    file_name: str = "",
) -> Path:
    """Saves a downloaded circular attachment (.pdf / .docx / .xlsx) to disk with automatic cache rotation."""
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_id = "".join(c for c in str(notice_id) if c.isalnum() or c in ("-", "_"))
    if file_name:
        clean_name = re.sub(r'[<>:"/\\|?*]', "_", file_name).strip()
        target = output_dir / f"{safe_id}_{clean_name}"
    else:
        ext = ".pdf" if raw_bytes.startswith(b"%PDF") else (".docx" if raw_bytes.startswith(b"PK") else ".bin")
        target = output_dir / f"circolare_{safe_id}{ext}"
    _write_private_bytes(target, raw_bytes)
    _prune_attachments_dir(output_dir)
    return target


save_pdf_attachment = save_attachment_file
