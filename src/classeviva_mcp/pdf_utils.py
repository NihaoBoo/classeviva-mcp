from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
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


def extract_pdf_text(
    pdf_bytes: bytes,
    filter_keyword: str = "",
    max_chars: int = 14000,
) -> tuple[str, int, list[int]]:
    """Extracts selectable text from PDF bytes using PyMuPDF.
    If the PDF has > 2 pages and `filter_keyword` (e.g. '5AI') is provided,
    applies smart page filtering (always keeping Page 1 + pages matching the tolerant class regex).
    Returns: (extracted_text, total_pages, included_page_numbers_1based)
    """
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return "", 0, []

    rx = build_class_regex(filter_keyword) if filter_keyword.strip() else None
    all_pages: list[tuple[int, str]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_pages = len(doc)
        for idx, page in enumerate(doc):
            all_pages.append((idx + 1, page.get_text("text").strip()))

    if rx is not None and total_pages > 2:
        matched = [
            (pno, txt)
            for pno, txt in all_pages
            if pno == 1 or rx.search(txt)
        ]
        # Only narrow down if at least one page beyond page 1 matched (or page 1 itself matched)
        if any(rx.search(txt) for _, txt in matched):
            selected = matched
        else:
            selected = all_pages
    else:
        selected = all_pages

    included_nums = [pno for pno, _ in selected]
    formatted = [f"[Pagina {pno}/{total_pages}]\n{txt}" for pno, txt in selected if txt]
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
    output_dir.mkdir(parents=True, exist_ok=True)
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
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            if "word/document.xml" not in zf.namelist():
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
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    except Exception:
        return ""

    rx = build_class_regex(filter_keyword) if filter_keyword.strip() else None
    sheets_output: list[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        # 1. Unmerge cells and fill every cell in the range with the top-left value
        merged_ranges = list(ws.merged_cells.ranges)
        for m_range in merged_ranges:
            top_val = ws.cell(m_range.min_row, m_range.min_col).value
            ws.unmerge_cells(str(m_range))
            if top_val is not None:
                for r in range(m_range.min_row, m_range.max_row + 1):
                    for c in range(m_range.min_col, m_range.max_col + 1):
                        ws.cell(r, c).value = top_val

        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
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
            for r in rows:
                picked = [r[c] for c in keep_cols if c < len(r)]
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
        for r in rows[:120]:
            all_lines.append(" | ".join(c for c in r if c))
        sheets_output.append("\n".join(all_lines))

    return "\n\n".join(sheets_output).strip()


def extract_attachment_text(
    raw_bytes: bytes,
    file_name: str = "",
    filter_keyword: str = "",
) -> str:
    """Auto-detects PDF, DOCX, XLSX, CSV/TXT, or ZIP (with zip-bomb protection) and extracts text."""
    if not raw_bytes:
        return ""
    fname_low = (file_name or "").lower()

    if raw_bytes.startswith(b"%PDF") or fname_low.endswith(".pdf"):
        txt, _, _ = extract_pdf_text(raw_bytes, filter_keyword=filter_keyword)
        return txt

    if raw_bytes.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = zf.namelist()
                if "word/document.xml" in names or fname_low.endswith(".docx"):
                    return extract_docx_text(raw_bytes, filter_keyword=filter_keyword)
                if any(n.startswith("xl/") for n in names) or fname_low.endswith(".xlsx"):
                    return extract_xlsx_text(raw_bytes, filter_keyword=filter_keyword)

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
                    sub_bytes = zf.read(info.filename)
                    sub_txt = extract_attachment_text(sub_bytes, file_name=info.filename, filter_keyword=filter_keyword)
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


def save_attachment_file(
    raw_bytes: bytes,
    notice_id: str,
    output_dir: Path,
    file_name: str = "",
) -> Path:
    """Saves a downloaded circular attachment (.pdf / .docx / .xlsx) to disk with automatic cache rotation."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in str(notice_id) if c.isalnum() or c in ("-", "_"))
    if file_name:
        clean_name = re.sub(r'[<>:"/\\|?*]', "_", file_name).strip()
        target = output_dir / f"{safe_id}_{clean_name}"
    else:
        ext = ".pdf" if raw_bytes.startswith(b"%PDF") else (".docx" if raw_bytes.startswith(b"PK") else ".bin")
        target = output_dir / f"circolare_{safe_id}{ext}"
    target.write_bytes(raw_bytes)
    _prune_attachments_dir(output_dir)
    return target



save_pdf_attachment = save_attachment_file
