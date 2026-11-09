"""Comprehensive, isolated, offline pytest suite for ClasseViva QoL Milestone 3.

Covers:
1. Didactics: list_didactics, download_didactics_item, PDF/DOCX text extraction,
   custom destination folder without pruning, upstream typo 'didacticts', nested vs flat formats.
2. Calendar & Periods: get_calendar with days_ahead and HD-only holidays filter,
   Italian weekday names, get_periods.
3. Disciplinary Notes: get_notes with categories (NTTE, NTCL, NTWN, NTST),
   lazy full-body fetching, breakdown counts.
4. Documents & Schoolbooks: get_schoolbooks with subject filtering and total cost calculation,
   get_documents and download_document.
5. Notice 404 Auto-Unlock: 404 attachment retry via POST mark_notice_read and read-only mode safety.
6. Snapshot Enrichment & Privacy: upcoming_holidays and didactics inclusion, recursive zero notes leakage.
7. MCP Native Prompts: recap-settimanale and piano-recupero-materie registration and read-only suppression.
8. Read-Only Mode Isolation: tool and prompt hiding in subshell and direct PermissionError guards.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest
from mcp.types import GetPromptResult, TextContent

from classeviva_mcp import server
from classeviva_mcp.client import SpaggiariClient
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import (
    CalendarDay,
    DidacticsFile,
    DidacticsFolder,
    DisciplinaryNote,
    GradeItem,
    SchoolBook,
    SchoolDocument,
    SchoolPeriod,
    StudentProfile,
    SubjectTeacher,
    now_rome,
    today_rome_str,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# Helpers & Fixtures
# ============================================================================


def _sample_pdf_bytes(text: str = "Test PDF Didattica") -> bytes:
    """Generates valid minimal in-memory PDF bytes containing specified text."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _sample_docx_bytes(text: str = "Test DOCX Didattica") -> bytes:
    """Generates valid minimal in-memory DOCX bytes containing specified text."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>'
            '</w:document>',
        )
    return buf.getvalue()


def _setup_mock_client(
    tmp_path: Path, handler: Any
) -> tuple[SpaggiariClient, CredentialStore, httpx.AsyncClient]:
    """Instantiates a real SpaggiariClient configured with httpx.MockTransport."""
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = SpaggiariClient(store=store, http_client=http_client)
    client._token = "mock-token-qol-m3"
    client._student_id = "1234567"
    client._ident = "S1234567X"
    client._first_name = "Mario"
    client._last_name = "Rossi"
    return client, store, http_client


def _recursively_find_forbidden_disciplinary_keys(
    data: Any, path: str = ""
) -> list[tuple[str, Any]]:
    """Traverse all dict keys and subkeys to locate any forbidden disciplinary note references."""
    forbidden_disciplinary_names = {
        "disciplinary_notes",
        "note_disciplinari",
        "teacher_notes",
        "class_notes",
        "sanctions",
    }
    forbidden_matches: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            curr_path = f"{path}.{k}" if path else str(k)
            if k.lower() in forbidden_disciplinary_names:
                forbidden_matches.append((curr_path, v))
            forbidden_matches.extend(_recursively_find_forbidden_disciplinary_keys(v, curr_path))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            curr_path = f"{path}[{idx}]"
            forbidden_matches.extend(_recursively_find_forbidden_disciplinary_keys(item, curr_path))
    return forbidden_matches


def _run_subshell_probe(env_var: str, value: str) -> dict[str, Any]:
    """Runs an isolated subshell to probe registered MCP tools and prompts."""
    env = os.environ.copy()
    env[env_var] = value
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    code = (
        "import asyncio, json;\n"
        "from classeviva_mcp import server;\n"
        "tools = [t.name for t in asyncio.run(server.mcp.list_tools())];\n"
        "prompts = [p.name for p in asyncio.run(server.mcp.list_prompts())];\n"
        "print(json.dumps({'tools': tools, 'prompts': prompts}))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout.strip())  # type: ignore[no-any-return]


# ============================================================================
# Dimension 1: Didactics Tests
# ============================================================================


@pytest.mark.asyncio
async def test_didactics_listing_and_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests list_didactics with subject/teacher filtering and download_didactics_item with PDF/DOCX extraction."""
    pdf_content = "Teorema fondamentale del calcolo integrale e derivate"
    docx_content = "Esperimento forza di Lorentz e induzione magnetica"
    pdf_bytes = _sample_pdf_bytes(pdf_content)
    docx_bytes = _sample_docx_bytes(docx_content)

    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path.endswith("/didactics"):
            return httpx.Response(
                200,
                json={
                    "didactics": [
                        {
                            "folderId": 101,
                            "folderName": "Matematica - Analisi",
                            "teacherName": "Prof.ssa Rossi",
                            "teacherId": 11,
                            "lastShareDT": "2026-11-01",
                            "files": [
                                {
                                    "contentId": 501,
                                    "contentName": "Calcolo_Integrale.pdf",
                                    "shareDate": "2026-11-01",
                                    "fileType": "pdf",
                                    "sizeBytes": len(pdf_bytes),
                                }
                            ],
                        },
                        {
                            "folderId": 102,
                            "folderName": "Fisica - Elettromagnetismo",
                            "teacherName": "Prof. Bianchi",
                            "teacherId": 12,
                            "lastShareDT": "2026-11-02",
                            "files": [
                                {
                                    "contentId": 502,
                                    "contentName": "Relazione_Lab.docx",
                                    "shareDate": "2026-11-02",
                                    "fileType": "docx",
                                    "sizeBytes": len(docx_bytes),
                                }
                            ],
                        },
                        {
                            "folderId": 103,
                            "folderName": "Matematica - Esercizi",
                            "teacherName": "Prof.ssa Verdi",
                            "teacherId": 13,
                            "lastShareDT": "2026-11-03",
                            "files": [
                                {
                                    "contentId": 503,
                                    "contentName": "Esercizi_1.pdf",
                                    "shareDate": "2026-11-03",
                                    "fileType": "pdf",
                                    "sizeBytes": 12000,
                                }
                            ],
                        },
                    ]
                },
            )
        if url_path.endswith("/didactics/item/501"):
            return httpx.Response(
                200,
                headers={"Content-Disposition": 'attachment; filename="Calcolo_Integrale.pdf"'},
                content=pdf_bytes,
            )
        if url_path.endswith("/didactics/item/502"):
            return httpx.Response(
                200,
                headers={"Content-Disposition": 'attachment; filename="Relazione_Lab.docx"'},
                content=docx_bytes,
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        # 1. Filter by subject: Matematica
        res_math = await server.list_didactics(subject="Matematica")
        assert res_math["folders_count"] == 2
        assert res_math["files_count"] == 2
        folder_names = [f["folder_name"] for f in res_math["folders"]]
        assert "Matematica - Analisi" in folder_names
        assert "Matematica - Esercizi" in folder_names

        # 2. Filter by teacher: Bianchi
        res_teacher = await server.list_didactics(teacher="Bianchi")
        assert res_teacher["folders_count"] == 1
        assert res_teacher["files_count"] == 1
        assert res_teacher["folders"][0]["teacher_name"] == "Prof. Bianchi"

        # 3. Filter with no match
        res_empty = await server.list_didactics(subject="Chimica")
        assert res_empty["folders_count"] == 0
        assert res_empty["files_count"] == 0
        assert "materia 'Chimica'" in res_empty["summary"]

        # 4. List all
        res_all = await server.list_didactics()
        assert res_all["folders_count"] == 3
        assert res_all["files_count"] == 3
        assert "3 cartelle e 3 file" in res_all["summary"]

        # 5. Download PDF item
        dl_pdf = await server.download_didactics_item(file_id=501)
        assert dl_pdf["file_id"] == 501
        assert dl_pdf["file_name"] == "Calcolo_Integrale.pdf"
        assert pdf_content in dl_pdf["text"]
        assert Path(dl_pdf["saved_path"]).exists()
        assert not dl_pdf["is_text_truncated"]

        # 6. Download DOCX item
        dl_docx = await server.download_didactics_item(file_id=502)
        assert dl_docx["file_id"] == 502
        assert dl_docx["file_name"] == "Relazione_Lab.docx"
        assert docx_content in dl_docx["text"]
        assert Path(dl_docx["saved_path"]).exists()
        assert not dl_docx["is_text_truncated"]


@pytest.mark.asyncio
async def test_didactics_custom_destination_no_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that download_didactics_item with custom destination_folder does NOT prune existing files."""
    custom_dest = tmp_path / "custom_user_folder"
    custom_dest.mkdir(parents=True, exist_ok=True)

    # Seed custom destination with 35 dummy files
    dummy_files: list[Path] = []
    for i in range(35):
        f = custom_dest / f"pre_existing_file_{i:02d}.dat"
        f.write_bytes(b"dummy data")
        dummy_files.append(f)

    pdf_bytes = _sample_pdf_bytes("Studio di Funzione")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/didactics/item/777"):
            return httpx.Response(
                200,
                headers={"Content-Disposition": 'attachment; filename="Studio_Funzione.pdf"'},
                content=pdf_bytes,
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        res = await server.download_didactics_item(file_id=777, destination_folder=str(custom_dest))
        saved_path = Path(res["saved_path"])
        assert saved_path.exists()
        assert saved_path.parent.resolve() == custom_dest.resolve()

        # All 35 pre-existing files MUST still exist (no pruning occurred)
        for f in dummy_files:
            assert f.exists(), f"Pre-existing file {f.name} was pruned unexpectedly!"
        assert len(list(custom_dest.iterdir())) == 36


@pytest.mark.asyncio
async def test_didactics_typo_and_nested_vs_flat_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests client parsing of upstream typo 'didacticts' and handles both nested and flat folder structures."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/didactics"):
            # Notice the typo 'didacticts' returned by upstream API
            return httpx.Response(
                200,
                json={
                    "didacticts": [
                        # Nested format (teacher contains folders)
                        {
                            "teacherName": "Prof. Gialli",
                            "teacherId": 21,
                            "folders": [
                                {
                                    "folderId": 201,
                                    "folderName": "Informatica Java",
                                    "lastShareDT": "2026-11-01",
                                    "contents": [
                                        {
                                            "contentId": 901,
                                            "contentName": "OOP.pdf",
                                            "sizeBytes": 5120,
                                            "lastShareDT": "2026-11-01",
                                        }
                                    ],
                                }
                            ],
                        },
                        # Flat format (folder directly at top level)
                        {
                            "folderId": 202,
                            "folderName": "Sistemi Operativi",
                            "teacherName": "Prof. Neri",
                            "teacherId": 22,
                            "lastShareDate": "2026-11-02",
                            "contents": [
                                {
                                    "contentId": 902,
                                    "contentName": "Linux_Kernel.pdf",
                                    "size": 8192,
                                    "shareDate": "2026-11-02",
                                }
                            ],
                        },
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    async with http_client:
        folders = await client.get_didactics()
        assert len(folders) == 2

        f1 = next(f for f in folders if f.folder_name == "Informatica Java")
        assert f1.teacher_name == "Prof. Gialli"
        assert len(f1.files) == 1
        assert f1.files[0].content_name == "OOP.pdf"
        assert f1.files[0].size_bytes == 5120

        f2 = next(f for f in folders if f.folder_name == "Sistemi Operativi")
        assert f2.teacher_name == "Prof. Neri"
        assert len(f2.files) == 1
        assert f2.files[0].content_name == "Linux_Kernel.pdf"
        assert f2.files[0].size_bytes == 8192


# ============================================================================
# Dimension 2: Calendar & Periods Tests
# ============================================================================


@pytest.mark.asyncio
async def test_calendar_holidays_and_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests get_calendar filtering (HD status only, Italian weekdays) and client get_periods."""
    today = now_rome().date()
    d_holiday_1 = today + timedelta(days=5)
    d_lesson = today + timedelta(days=6)
    d_holiday_2 = today + timedelta(days=20)
    d_out_range = today + timedelta(days=70)

    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path.endswith("/calendar/all"):
            return httpx.Response(
                200,
                json={
                    "calendar": [
                        {
                            "dayDate": d_holiday_1.strftime("%Y-%m-%d"),
                            "dayOfWeek": 2,  # Martedì
                            "dayStatus": "HD",
                            "desc": "Festa del Patrono",
                        },
                        {
                            "dayDate": d_lesson.strftime("%Y%m%d"),  # Test 8-digit format
                            "dayOfWeek": 3,
                            "dayStatus": "WD",  # School day - must NOT be in holidays!
                            "desc": "Lezione Ordinaria",
                        },
                        {
                            "dayDate": d_holiday_2.strftime("%Y-%m-%d"),
                            "dayOfWeek": 5,  # Venerdì
                            "dayStatus": "HD",
                            "desc": "Ponte d'Autunno",
                        },
                        {
                            "dayDate": d_out_range.strftime("%Y-%m-%d"),
                            "dayOfWeek": 1,
                            "dayStatus": "HD",
                            "desc": "Vacanza Fuori Range",
                        },
                    ]
                },
            )
        if url_path.endswith("/periods"):
            return httpx.Response(
                200,
                json={
                    "periods": [
                        {
                            "periodCode": "T1",
                            "periodPos": 1,
                            "periodDesc": "Primo Trimestre",
                            "isFinal": False,
                            "dateStart": "2026-09-12",
                            "dateEnd": "2026-12-22",
                        },
                        {
                            "periodCode": "P2",
                            "periodPos": 2,
                            "periodDesc": "Pentamestre",
                            "isFinal": True,
                            "dateStart": "2027-01-07",
                            "dateEnd": "2027-06-10",
                        },
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        # 1. Test get_calendar tool with 30 days window
        cal_res = await server.get_calendar(days_ahead=30)
        assert cal_res["days_ahead"] == 30
        assert cal_res["holidays_count"] == 2

        holidays = cal_res["holidays"]
        assert len(holidays) == 2
        # Verify status is strictly HD
        assert all(h["status"] == "HD" for h in holidays)
        # Verify Italian weekday names
        assert holidays[0]["day_of_week"] == "Martedì"
        assert holidays[0]["description"] == "Festa del Patrono"
        assert holidays[1]["day_of_week"] == "Venerdì"
        assert holidays[1]["description"] == "Ponte d'Autunno"
        assert "Trovati 2 giorni festivi/ponti" in cal_res["summary"]

        # 2. Test get_periods client method
        periods = await client.get_periods()
        assert len(periods) == 2
        assert isinstance(periods[0], SchoolPeriod)
        assert periods[0].period_code == "T1"
        assert periods[0].period_pos == 1
        assert not periods[0].is_final
        assert periods[0].date_start == "2026-09-12"
        assert periods[0].date_end == "2026-12-22"

        assert isinstance(periods[1], SchoolPeriod)
        assert periods[1].period_code == "P2"
        assert periods[1].period_pos == 2
        assert periods[1].is_final


# ============================================================================
# Dimension 3: Disciplinary Notes Tests
# ============================================================================


@pytest.mark.asyncio
async def test_disciplinary_notes_full_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests get_notes tool with all 4 categories, verifying lazy body fetch and breakdown counts."""
    lazy_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if request.method == "GET" and url_path.endswith("/notes/all"):
            return httpx.Response(
                200,
                json={
                    "NTTE": [
                        {
                            "evtId": "1001",
                            "evtDate": "2026-11-05",
                            "authorName": "Prof. Verdi",
                            "evtText": "",  # Empty body: must trigger lazy POST fetch
                        }
                    ],
                    "NTCL": [
                        {
                            "evtId": "1002",
                            "evtDate": "2026-11-06",
                            "authorName": "Prof.ssa Neri",
                            "evtText": "La classe disturba la spiegazione.",  # Has body: no fetch
                        }
                    ],
                    "NTWN": [
                        {
                            "evtId": "1003",
                            "evtDate": "2026-11-07",
                            "authorName": "Vicepreside",
                            "evtText": "",  # Empty body: must trigger lazy POST fetch
                        }
                    ],
                    "NTST": [
                        {
                            "evtId": "1004",
                            "evtDate": "2026-11-08",
                            "authorName": "Consiglio di Classe",
                            "evtText": "Sospensione per 2 giorni.",  # Has body: no fetch
                        }
                    ],
                },
            )
        if request.method == "POST" and "/notes/NTTE/read/1001" in url_path:
            lazy_calls.append("NTTE/1001")
            return httpx.Response(
                200,
                json={"item": {"evtText": "Lo studente usa lo smartphone durante la lezione."}},
            )
        if request.method == "POST" and "/notes/NTWN/read/1003" in url_path:
            lazy_calls.append("NTWN/1003")
            return httpx.Response(
                200,
                json={"note": {"evtText": "Richiamo formale per 5 ritardi consecutivi."}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        res = await server.get_notes()
        assert res["count"] == 4

        # Breakdown counts
        breakdown = res["breakdown"]
        assert breakdown["teacher_notes"] == 1
        assert breakdown["class_notes"] == 1
        assert breakdown["warnings"] == 1
        assert breakdown["sanctions"] == 1

        # Check lazy fetch calls
        assert lazy_calls == ["NTTE/1001", "NTWN/1003"]

        notes = {n["id"]: n for n in res["notes"]}
        assert notes["1001"]["text"] == "Lo studente usa lo smartphone durante la lezione."
        assert notes["1001"]["is_read"] is True
        assert notes["1001"]["kind"] == "NTTE"

        assert notes["1002"]["text"] == "La classe disturba la spiegazione."
        assert notes["1002"]["kind"] == "NTCL"

        assert notes["1003"]["text"] == "Richiamo formale per 5 ritardi consecutivi."
        assert notes["1003"]["is_read"] is True
        assert notes["1003"]["kind"] == "NTWN"

        assert notes["1004"]["text"] == "Sospensione per 2 giorni."
        assert notes["1004"]["kind"] == "NTST"


# ============================================================================
# Dimension 4: Documents & Schoolbooks Tests
# ============================================================================


@pytest.mark.asyncio
async def test_schoolbooks_and_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests get_schoolbooks with subject filtering & total cost, plus get_documents & download_document."""
    doc_pdf_bytes = _sample_pdf_bytes("Scheda di Valutazione Ufficiale 1° Quadrimestre")

    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if request.method == "GET" and url_path.endswith("/schoolbooks"):
            return httpx.Response(
                200,
                json={
                    "schoolbooks": [
                        {
                            "isbn": "9788808111111",
                            "title": "Algoritmi e Strutture Dati",
                            "authors": "Cormen, Leiserson",
                            "publisher": "MIT Press",
                            "subject": "Informatica",
                            "price": "42,50",  # Test comma decimal string
                            "toBuy": True,
                        },
                        {
                            "isbn": "9788808222222",
                            "title": "Reti di Calcolatori",
                            "authors": "Tanenbaum",
                            "publisher": "Pearson",
                            "subject": "Sistemi e Reti",
                            "price": 38.00,  # Test float
                            "toBuy": True,
                        },
                        {
                            "isbn": "9788808333333",
                            "title": "Grammatica Italiana",
                            "authors": "Serianni",
                            "publisher": "UTET",
                            "subject": "Italiano",
                            "price": 20.00,
                            "toBuy": False,
                            "isRecommended": True,
                        },
                    ]
                },
            )
        if request.method == "POST" and url_path.endswith("/documents"):
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "hash": "hash_pagella_q1",
                            "desc": "Pagella 1° Quadrimestre",
                            "docType": "pagella",
                            "pubDT": "2027-02-15",
                        }
                    ]
                },
            )
        if request.method == "POST" and "/documents/read/hash_pagella_q1" in url_path:
            return httpx.Response(200, content=doc_pdf_bytes)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        # 1. Schoolbooks - all
        res_all_books = await server.get_schoolbooks()
        assert res_all_books["count"] == 3
        assert res_all_books["to_buy_count"] == 2
        # Total cost: 42.50 + 38.00 = 80.50
        assert res_all_books["total_cost_to_buy"] == 80.50
        assert "80.50 €" in res_all_books["summary"]

        # Client method test
        client_books = await client.get_schoolbooks()
        assert len(client_books) == 3
        assert isinstance(client_books[0], SchoolBook)

        # 2. Schoolbooks - filtered by subject
        res_math_books = await server.get_schoolbooks(subject="Informatica")
        assert res_math_books["count"] == 1
        assert res_math_books["to_buy_count"] == 1
        assert res_math_books["total_cost_to_buy"] == 42.50

        # 3. Documents - listing
        docs_res = await server.get_documents()
        assert docs_res["count"] == 1
        doc_item = docs_res["documents"][0]
        assert doc_item["id"] == "hash_pagella_q1"
        assert doc_item["title"] == "Pagella 1° Quadrimestre"
        assert doc_item["doc_type"] == "pagella"

        # Client method test
        client_docs = await client.get_documents()
        assert len(client_docs) == 1
        assert isinstance(client_docs[0], SchoolDocument)

        # 4. Documents - client download
        downloaded = await client.download_document("hash_pagella_q1")
        assert downloaded == doc_pdf_bytes
        assert downloaded.startswith(b"%PDF")


# ============================================================================
# Dimension 5: Notice 404 Auto-Unlock Tests
# ============================================================================


@pytest.mark.asyncio
async def test_read_notice_pdf_auto_unlock_on_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests that read_notice_pdf auto-unlocks on 404 attachment error, retries download, and sets notice_marked_as_read."""
    pdf_bytes = _sample_pdf_bytes("Contenuto Circolare Sbloccata dopo 404")
    attach_attempts = 0
    post_read_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attach_attempts
        url_path = request.url.path

        if request.method == "GET" and url_path.endswith("/noticeboard"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "pubId": "999",
                            "cntId": "999",
                            "cntTitle": "Circolare Uscita Didattica",
                            "pubDT": "2026-11-09",
                            "readStatus": False,  # unread initially
                            "attachments": [{"attachNum": 1, "fileName": "circolare_999.pdf"}],
                        }
                    ]
                },
            )
        if request.method == "GET" and "/noticeboard/attach/CF/999/1" in url_path:
            attach_attempts += 1
            if attach_attempts == 1:
                # First attempt fails with 404 Not Found
                return httpx.Response(404, text="Not Found - Circolare bloccata")
            # Second attempt succeeds after unlock
            return httpx.Response(200, content=pdf_bytes)

        if request.method == "POST" and "/noticeboard/read/CF/999/101" in url_path:
            post_read_calls.append("POST /read/101")
            return httpx.Response(200, json={"item": {"cntText": "Circolare sbloccata con successo"}})

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)

    async with http_client:
        res = await server.read_notice_pdf(notice_id="999", event_code="CF", allow_mark_as_read=True)
        assert res["notice_marked_as_read"] is True
        assert "Contenuto Circolare Sbloccata dopo 404" in res["text"]
        assert attach_attempts == 2
        assert len(post_read_calls) >= 1


@pytest.mark.asyncio
async def test_read_notice_pdf_404_read_only_mode_no_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that in read-only mode, 404 does NOT call the POST unlock endpoint."""
    post_read_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if request.method == "GET" and url_path.endswith("/noticeboard"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "pubId": "999",
                            "cntId": "999",
                            "cntTitle": "Circolare Uscita Didattica",
                            "pubDT": "2026-11-09",
                            "readStatus": False,
                            "attachments": [{"attachNum": 1, "fileName": "circolare_999.pdf"}],
                        }
                    ]
                },
            )
        if request.method == "POST" and "/noticeboard/read" in url_path:
            post_read_calls.append(url_path)
            return httpx.Response(200, json={"item": {}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, store, http_client = _setup_mock_client(tmp_path, handler)
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)
    server.set_client_for_testing(client, store)

    async with http_client:
        res = await server.read_notice_pdf(notice_id="999", event_code="CF", allow_mark_as_read=True)
        # Because notice is unread and read-only mode is active, execution skips before remote modification
        assert res["notice_marked_as_read"] is False
        assert res["attachments_count"] == 0
        assert "In modalità read-only questa operazione non è consentita" in res["text"]
        # CRITICAL SECURITY ASSERTION: No POST requests were made!
        assert len(post_read_calls) == 0

        # Also verify client method directly
        raw_client = await client.read_notice_content(
            event_code="CF", pub_id="999", allow_mark_as_read=False
        )
        assert raw_client["skipped"] is True
        assert raw_client["marked_as_read"] is False


# ============================================================================
# Dimension 6: Snapshot Enrichment & Privacy Tests
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_enriched_without_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests get_snapshot enrichment with upcoming_holidays & didactics, and strictly asserts zero notes leakage."""
    today = now_rome().date()
    d_holiday = today + timedelta(days=3)

    class ComprehensiveFakeClient:
        async def get_profile(self) -> StudentProfile:
            return StudentProfile(
                ident="S1234567X",
                student_id="1234567",
                account_type="student",
                first_name="Marco",
                school_class="5AI",
                school_name="ITIS Galilei",
            )

        async def get_subjects(self) -> list[SubjectTeacher]:
            return [
                SubjectTeacher(subject_id="s1", subject_name="Informatica", teachers=["Rossi"]),
            ]

        async def get_grades(self, subject_filter: str | None = None) -> list[GradeItem]:
            return [
                GradeItem(
                    id="g1",
                    subject="Informatica",
                    value=8.0,
                    display_value="8",
                    weight=1.0,
                    date=today_rome_str(),
                )
            ]

        async def get_absences(self) -> list[Any]:
            return []

        async def get_agenda(self, start_date: str, end_date: str) -> list[Any]:
            return []

        async def get_calendar(self) -> list[CalendarDay]:
            return [
                CalendarDay(
                    date=d_holiday.strftime("%Y-%m-%d"),
                    day_of_week=d_holiday.isoweekday(),
                    status="HD",
                    is_holiday=True,
                    description="Festa Nazionale",
                )
            ]

        async def get_didactics(self) -> list[DidacticsFolder]:
            return [
                DidacticsFolder(
                    folder_id=1,
                    folder_name="Materiali Informatica",
                    teacher_name="Prof. Rossi",
                    teacher_id=10,
                    last_share_date="2026-11-08",
                    files=[
                        DidacticsFile(
                            content_id=101,
                            content_name="Appunti.pdf",
                            share_date="2026-11-08",
                            file_type="pdf",
                            size_bytes=4096,
                        )
                    ],
                )
            ]

        async def get_notes(self, period_filter: str | None = None) -> list[DisciplinaryNote]:
            # Present on client to prove snapshot MUST NOT leak them
            return [
                DisciplinaryNote(
                    id="nt1",
                    kind="NTTE",
                    kind_description="Nota del docente",
                    date="2026-11-05",
                    teacher="Prof. Rossi",
                    text="Nota disciplinare privata e confidenziale",
                    is_read=True,
                )
            ]

    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(ComprehensiveFakeClient(), store)  # type: ignore[arg-type]

    snapshot = await server.get_snapshot(days_ahead=14)

    # 1. Enriched sections present
    assert "upcoming_holidays" in snapshot
    assert snapshot["upcoming_holidays"]["count"] == 1
    assert snapshot["upcoming_holidays"]["holidays"][0]["description"] == "Festa Nazionale"

    assert "didactics" in snapshot
    assert snapshot["didactics"]["folders_count"] == 1
    assert snapshot["didactics"]["files_count"] == 1
    assert snapshot["didactics"]["new_didactics_count"] == 1

    # 2. Assert zero tolerance for disciplinary notes in top-level keys
    assert "notes" not in snapshot, "CRITICAL: 'notes' key leaked into snapshot root!"
    assert "disciplinary_notes" not in snapshot, "CRITICAL: 'disciplinary_notes' key leaked into snapshot root!"

    # 3. Assert zero tolerance across all major section dictionaries
    for section_name in (
        "profile",
        "student_context",
        "grades",
        "absences",
        "homework",
        "agenda",
        "upcoming_holidays",
        "didactics",
    ):
        sec = snapshot.get(section_name)
        if isinstance(sec, dict):
            assert "notes" not in sec, f"CRITICAL: 'notes' section key leaked into snapshot['{section_name}']"
            assert "disciplinary_notes" not in sec, (
                f"CRITICAL: 'disciplinary_notes' key leaked into snapshot['{section_name}']"
            )

    # 4. Strict recursive privacy check
    leaks = _recursively_find_forbidden_disciplinary_keys(snapshot)
    assert not leaks, f"CRITICAL: Disciplinary notes leaked into snapshot: {leaks}"

    # 5. Assert disciplinary note text does not appear anywhere in serialized snapshot
    serialized_snapshot = json.dumps(snapshot)
    assert "Nota disciplinare privata" not in serialized_snapshot
    assert "confidenziale" not in serialized_snapshot


# ============================================================================
# Dimension 7: MCP Native Prompts Tests
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_prompts_recap_and_recupero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that recap-settimanale and piano-recupero-materie are registered in personal mode and produce valid messages."""
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    # 1. Verify prompt registration
    prompts = await server.mcp.list_prompts()
    prompt_names = [p.name for p in prompts]
    assert "recap-settimanale" in prompt_names
    assert "piano-recupero-materie" in prompt_names

    # 2. Test recap-settimanale prompt execution
    recap_res = await server.mcp.get_prompt("recap-settimanale", {"days_ahead": "7"})
    assert isinstance(recap_res, GetPromptResult)
    assert len(recap_res.messages) == 1
    recap_content = recap_res.messages[0].content
    assert isinstance(recap_content, TextContent)
    recap_text = recap_content.text
    assert "prossimi 7 giorni" in recap_text
    assert "get_today_context" in recap_text
    assert "get_homework(days_ahead=7)" in recap_text
    assert "get_calendar(days_ahead=7)" in recap_text
    assert "list_notices(limit=10)" in recap_text

    # 3. Test piano-recupero-materie prompt execution
    recupero_res = await server.mcp.get_prompt("piano-recupero-materie", {"subject": "Matematica"})
    assert isinstance(recupero_res, GetPromptResult)
    assert len(recupero_res.messages) == 1
    recupero_content = recupero_res.messages[0].content
    assert isinstance(recupero_content, TextContent)
    recupero_text = recupero_content.text
    assert "materia 'Matematica'" in recupero_text
    assert "get_grades" in recupero_text
    assert "calculate_grade_target" in recupero_text
    assert "get_lessons" in recupero_text
    assert "get_agenda" in recupero_text


def test_mcp_prompts_suppressed_in_read_only_mode() -> None:
    """Verifies via subshell probe that personal prompts are suppressed in read-only mode."""
    res = _run_subshell_probe("CLASSEVIVA_READ_ONLY_MODE", "1")
    prompts = set(res["prompts"])
    assert "recap-settimanale" not in prompts
    assert "piano-recupero-materie" not in prompts
    assert "setup" not in prompts
    assert "daily_brief" not in prompts
    assert "recovery_plan" not in prompts
    assert prompts == {"notices_brief"}


# ============================================================================
# Dimension 8: Read-Only Mode Isolation Tests
# ============================================================================


def test_read_only_mode_hides_all_new_qol_tools() -> None:
    """Verifies that none of the 6 new QoL tools are exposed in read-only mode."""
    res = _run_subshell_probe("CLASSEVIVA_READ_ONLY_MODE", "1")
    tools = set(res["tools"])

    new_qol_tools = {
        "list_didactics",
        "download_didactics_item",
        "get_calendar",
        "get_notes",
        "get_documents",
        "get_schoolbooks",
    }
    assert not (new_qol_tools & tools), f"QoL tools leaked in read-only mode: {new_qol_tools & tools}"
    assert "get_snapshot" not in tools
    assert tools == {"get_today_context", "list_notices", "read_notice_pdf"}


@pytest.mark.asyncio
async def test_read_only_mode_direct_invocation_raises_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that direct invocations of all 6 new tools raise PermissionError when READ_ONLY_MODE=True."""
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)

    with pytest.raises(PermissionError):
        await server.list_didactics()

    with pytest.raises(PermissionError):
        await server.download_didactics_item(file_id=1)

    with pytest.raises(PermissionError):
        await server.get_calendar()

    with pytest.raises(PermissionError):
        await server.get_notes()

    with pytest.raises(PermissionError):
        await server.get_documents()

    with pytest.raises(PermissionError):
        await server.get_schoolbooks()
