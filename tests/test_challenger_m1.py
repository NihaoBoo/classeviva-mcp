from pathlib import Path
from typing import Any

import httpx
import pytest

from classeviva_mcp.client import (
    MAX_ATTACHMENT_BYTES,
    ClasseVivaError,
    SpaggiariClient,
)
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import (
    CalendarDay,
    DidacticsFile,
    DidacticsFolder,
)


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)


def _client(tmp_path: Path, handler: Any) -> tuple[SpaggiariClient, httpx.AsyncClient]:
    store = _store(tmp_path)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpaggiariClient(store=store, http_client=http_client)
    client._token = "test-token"
    client._student_id = "1234567"
    return client, http_client


# ============================================================================
# 1. Didactics & Didacticts tests
# ============================================================================

@pytest.mark.asyncio
async def test_didactics_nested_with_standard_key(tmp_path: Path) -> None:
    payload = {
        "didactics": [
            {
                "teacherId": 101,
                "teacherName": "Prof. Mario Rossi",
                "folders": [
                    {
                        "folderId": 1001,
                        "folderName": "Analisi Matematica",
                        "lastShareDT": "2026-10-15T10:00:00",
                        "contents": [
                            {
                                "contentId": 5001,
                                "contentName": "Limiti_e_Derivate.pdf",
                                "shareDate": "2026-10-15",
                                "fileType": "pdf",
                                "sizeBytes": 2048576,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/students/1234567/didactics"
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        folders = await client.get_didactics()

    assert len(folders) == 1
    f = folders[0]
    assert isinstance(f, DidacticsFolder)
    assert f.folder_id == 1001
    assert f.folder_name == "Analisi Matematica"
    assert f.teacher_name == "Prof. Mario Rossi"
    assert f.teacher_id == 101
    assert f.last_share_date == "2026-10-15"
    assert len(f.files) == 1
    doc = f.files[0]
    assert isinstance(doc, DidacticsFile)
    assert doc.content_id == 5001
    assert doc.content_name == "Limiti_e_Derivate.pdf"
    assert doc.file_type == "pdf"
    assert doc.size_bytes == 2048576
    assert doc.share_date == "2026-10-15"


@pytest.mark.asyncio
async def test_didactics_flat_with_typo_key(tmp_path: Path) -> None:
    # Test "didacticts" (typo by Spaggiari) and flat structure
    payload = {
        "didacticts": [
            {
                "folderId": "F2002",
                "folderName": "Fisica Moderna",
                "teacherName": "laura bianchi",
                "teacherId": "T202",
                "lastShareDate": "2026-11-01",
                "files": [
                    {
                        "id": "C9001",
                        "name": "Relativita.docx",
                        "lastShareDT": "2026-11-01T12:00:00",
                        "type": "docx",
                        "size": 102400,
                    }
                ],
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        folders = await client.get_didactics()

    assert len(folders) == 1
    f = folders[0]
    assert f.folder_id == "F2002"
    assert f.folder_name == "Fisica Moderna"
    assert f.teacher_name == "Laura Bianchi"
    assert f.teacher_id == "T202"
    assert f.last_share_date == "2026-11-01"
    assert len(f.files) == 1
    doc = f.files[0]
    assert doc.content_id == "C9001"
    assert doc.content_name == "Relativita.docx"
    assert doc.file_type == "docx"
    assert doc.size_bytes == 102400
    assert doc.share_date == "2026-11-01"


@pytest.mark.asyncio
async def test_didactics_raw_list_payload(tmp_path: Path) -> None:
    # Test payload returned directly as a list
    payload = [
        {
            "folderId": 3003,
            "name": "Storia Contemporanea",
            "files": [],
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        folders = await client.get_didactics()

    assert len(folders) == 1
    assert folders[0].folder_id == 3003
    assert folders[0].folder_name == "Storia Contemporanea"
    assert folders[0].files == []


# ============================================================================
# 2. Download didactics & document: byte limits & redirect security
# ============================================================================

@pytest.mark.asyncio
async def test_download_didactics_file_normal_success(tmp_path: Path) -> None:
    fake_bytes = b"%PDF-1.4 test content"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/students/1234567/didactics/item/5001"
        return httpx.Response(
            200,
            content=fake_bytes,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="appunti.pdf"',
            },
        )

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        data, fname, ctype = await client.download_didactics_file(5001)

    assert data == fake_bytes
    assert fname == "appunti.pdf"
    assert ctype == "application/pdf"


@pytest.mark.asyncio
async def test_download_didactics_file_exceeds_content_length(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"too large header",
            headers={"content-length": str(MAX_ATTACHMENT_BYTES + 1024)},
        )

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="Allegato troppo grande"):
            await client.download_didactics_file(5001)


@pytest.mark.asyncio
async def test_download_didactics_file_exceeds_streaming_bytes(tmp_path: Path) -> None:
    # Stream bytes without content-length header
    oversized_data = b"x" * 2000

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized_data)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        # Pass a low max_bytes to verify stream cut-off
        with pytest.raises(ClasseVivaError, match="Allegato troppo grande"):
            await client.download_didactics_file(5001, max_bytes=1000)


@pytest.mark.asyncio
async def test_download_didactics_file_redirect_to_allowed_cdn(tmp_path: Path) -> None:
    cdn_bytes = b"cdn content"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/v1/students/1234567/didactics/item/5001":
            return httpx.Response(
                302,
                headers={"Location": "https://cdndoc.spaggiari.eu/files/doc.pdf"},
            )
        if (
            request.url.host == "cdndoc.spaggiari.eu"
            and request.url.path == "/files/doc.pdf"
        ):
            # Verify no Z-Auth-Token header leaked to CDN
            assert "z-auth-token" not in request.headers
            return httpx.Response(
                200,
                content=cdn_bytes,
                headers={"content-type": "application/pdf"},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        data, fname, ctype = await client.download_didactics_file(5001)

    assert data == cdn_bytes
    assert fname == "doc.pdf"
    assert ctype == "application/pdf"


@pytest.mark.asyncio
async def test_download_didactics_file_rejects_malicious_redirect(tmp_path: Path) -> None:
    malicious_targets = [
        "https://evil.com/malware.exe",
        "https://cdndoc.spaggiari.eu.attacker.com/fake.pdf",
        "https://evil-spaggiari.eu/phish.pdf",
        "http://cdndoc.spaggiari.eu/unencrypted.pdf",
    ]

    for target in malicious_targets:
        async def handler(request: httpx.Request, loc: str = target) -> httpx.Response:
            return httpx.Response(302, headers={"Location": loc})

        client, http_client = _client(tmp_path, handler)
        async with http_client:
            with pytest.raises(ClasseVivaError, match="Redirect didattica verso host non consentito"):
                await client.download_didactics_file(5001)


@pytest.mark.asyncio
async def test_download_document_redirect_security_and_limits(tmp_path: Path) -> None:
    # 1. Allowed redirect
    async def handler_ok(request: httpx.Request) -> httpx.Response:
        if "/documents/read/" in request.url.path:
            return httpx.Response(
                302,
                headers={"Location": "https://cdndoc.spaggiari.eu/reports/pagella.pdf"},
            )
        if request.url.host == "cdndoc.spaggiari.eu":
            return httpx.Response(200, content=b"PAGELLA CONTENT")
        raise AssertionError(f"Unexpected request: {request.url}")

    client, http_client = _client(tmp_path, handler_ok)
    async with http_client:
        data = await client.download_document("hash123")
    assert data == b"PAGELLA CONTENT"

    # 2. Blocked malicious redirect
    async def handler_evil(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.org/steal"})

    client, http_client = _client(tmp_path, handler_evil)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="Redirect documento verso host non consentito"):
            await client.download_document("hash123")


# ============================================================================
# 3. CalendarDay model validator & get_calendar tests
# ============================================================================

def test_calendar_day_model_holiday_trigger() -> None:
    # Status "HD" must trigger is_holiday=True
    hd_day = CalendarDay(date="2026-12-25", day_of_week=5, status="HD")
    assert hd_day.is_holiday is True

    # Status "HD" with is_holiday=False explicitly supplied must still be forced True by validator
    forced_hd = CalendarDay(date="2026-12-25", day_of_week=5, status="HD", is_holiday=False)
    assert forced_hd.is_holiday is True

    # Status "WD" (working day) must be False by default
    wd_day = CalendarDay(date="2026-12-22", day_of_week=2, status="WD")
    assert wd_day.is_holiday is False

    # Status "SD" (school day) must be False by default
    sd_day = CalendarDay(date="2026-12-23", day_of_week=3, status="SD")
    assert sd_day.is_holiday is False

    # Status "NW" (non-working / weekend) must be False by default
    nw_day = CalendarDay(date="2026-12-27", day_of_week=7, status="NW")
    assert nw_day.is_holiday is False


@pytest.mark.asyncio
async def test_get_calendar_parsing_and_sorting(tmp_path: Path) -> None:
    payload = {
        "calendar": [
            {
                "dayDate": "20261225",  # 8-digit format YYYYMMDD
                "dayOfWeek": 5,
                "dayStatus": "HD",
                "desc": "Natale",
            },
            {
                "date": "2026-12-22",  # Standard YYYY-MM-DD
                "dow": 2,
                "status": "WD",
                "description": "Lezione ordinaria",
            },
            {
                "dayDate": "20261224",
                "dayOfWeek": 4,
                "dayStatus": "SD",
                "desc": "Vigilia",
            },
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/students/1234567/calendar/all"
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        days = await client.get_calendar()

    # Must be sorted chronologically
    assert len(days) == 3
    assert [d.date for d in days] == ["2026-12-22", "2026-12-24", "2026-12-25"]

    d22 = days[0]
    assert d22.date == "2026-12-22"
    assert d22.status == "WD"
    assert d22.is_holiday is False

    d24 = days[1]
    assert d24.date == "2026-12-24"
    assert d24.status == "SD"
    assert d24.is_holiday is False

    d25 = days[2]
    assert d25.date == "2026-12-25"
    assert d25.status == "HD"
    assert d25.is_holiday is True
    assert d25.description == "Natale"


# ============================================================================
# 4. Disciplinary Notes & Lazy POST read tests
# ============================================================================

@pytest.mark.asyncio
async def test_get_notes_categories_and_lazy_post(tmp_path: Path) -> None:
    notes_payload = {
        "NTTE": [
            {
                "evtId": 7001,
                "evtDate": "2026-11-05T09:00:00",
                "authorName": "Prof. Verdi",
                "evtText": "Lo studente dimentica il materiale di disegno.",
                "readStatus": True,
                "periodCode": "Q1",
            }
        ],
        "NTCL": [
            {
                "evtId": 7002,
                "evtDate": "2026-11-04T10:00:00",
                "authorName": "Prof. Neri",
                "evtText": "",  # Empty text -> triggers lazy POST
                "readStatus": False,
                "periodCode": "Q1",
            }
        ],
        "NTWN": [
            {
                "evtId": 7003,
                "evtDate": "2026-11-03T11:00:00",
                "authorName": "Preside",
                "evtText": "Richiamo per ritardo ingiustificato.",
                "readStatus": True,
                "periodCode": "Q1",
            }
        ],
        "NTST": [
            {
                "evtId": 7004,
                "evtDate": "2026-11-02T12:00:00",
                "authorName": "Consiglio di Classe",
                "evtText": "",  # Empty text -> triggers lazy POST
                "readStatus": False,
                "periodCode": "Q1",
            }
        ],
    }

    lazy_post_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/v1/students/1234567/notes/all":
            return httpx.Response(200, json=notes_payload)
        if request.url.path == "/rest/v1/students/1234567/notes/NTCL/read/7002":
            lazy_post_calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"item": {"evtText": "La classe disturba la lezione."}},
            )
        if request.url.path == "/rest/v1/students/1234567/notes/NTST/read/7004":
            lazy_post_calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"note": {"text": "Sospensione per comportamento scorretto."}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        notes = await client.get_notes()

    # Both empty notes must have triggered lazy POST
    assert len(lazy_post_calls) == 2
    assert any("/notes/NTCL/read/7002" in call for call in lazy_post_calls)
    assert any("/notes/NTST/read/7004" in call for call in lazy_post_calls)

    assert len(notes) == 4
    # Must be sorted by date descending: 7001 (Nov 5), 7002 (Nov 4), 7003 (Nov 3), 7004 (Nov 2)
    assert [n.id for n in notes] == ["7001", "7002", "7003", "7004"]

    n1, n2, n3, n4 = notes
    assert n1.kind == "NTTE"
    assert n1.kind_description == "Nota del docente"
    assert n1.text == "Lo studente dimentica il materiale di disegno."

    assert n2.kind == "NTCL"
    assert n2.kind_description == "Nota di classe"
    assert n2.text == "La classe disturba la lezione."
    assert n2.is_read is True

    assert n3.kind == "NTWN"
    assert n3.kind_description == "Richiamo"
    assert n3.text == "Richiamo per ritardo ingiustificato."

    assert n4.kind == "NTST"
    assert n4.kind_description == "Sanzione disciplinare"
    assert n4.text == "Sospensione per comportamento scorretto."
    assert n4.is_read is True


@pytest.mark.asyncio
async def test_get_notes_lazy_post_error_resilience(tmp_path: Path) -> None:
    notes_payload = {
        "NTTE": [
            {
                "evtId": 7005,
                "evtDate": "2026-11-06",
                "evtText": "",  # triggers POST which fails with 500
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/v1/students/1234567/notes/all":
            return httpx.Response(200, json=notes_payload)
        if "/notes/NTTE/read/7005" in request.url.path:
            return httpx.Response(500, text="Internal Server Error")
        raise AssertionError(f"Unexpected: {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        # Should NOT raise an unhandled exception, but return note with empty text gracefully
        notes = await client.get_notes()

    assert len(notes) == 1
    assert notes[0].id == "7005"
    assert notes[0].text == ""


# ============================================================================
# 5. Additional M1 coverage: Periods, Documents, Schoolbooks, 404 Auto-Unlock
# ============================================================================

@pytest.mark.asyncio
async def test_get_periods_sorted_by_pos(tmp_path: Path) -> None:
    payload = {
        "periods": [
            {"periodCode": "Q2", "periodPos": 2, "periodDesc": "Secondo Quadrimestre"},
            {"periodCode": "Q1", "periodPos": 1, "periodDesc": "Primo Quadrimestre"},
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        periods = await client.get_periods()

    assert len(periods) == 2
    assert [p.period_pos for p in periods] == [1, 2]
    assert [p.period_code for p in periods] == ["Q1", "Q2"]


@pytest.mark.asyncio
async def test_get_documents_parsing(tmp_path: Path) -> None:
    payload = {
        "documents": [
            {"docHash": "hash_pagella", "desc": "Pagella 1Q", "docType": "pagella", "pubDT": "2026-02-10"}
        ],
        "schoolReports": [
            {"id": "hash_cert", "title": "Certificato Iscrizione", "date": "2026-01-15"}
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/rest/v1/students/1234567/documents"
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        docs = await client.get_documents()

    assert len(docs) == 2
    assert docs[0].hash == "hash_pagella"
    assert docs[0].title == "Pagella 1Q"
    assert docs[1].hash == "hash_cert"
    assert docs[1].title == "Certificato Iscrizione"


@pytest.mark.asyncio
async def test_get_schoolbooks_parsing_and_filter(tmp_path: Path) -> None:
    payload = {
        "schoolbooks": [
            {
                "isbn": "9788808123456",
                "title": "Matematica Blu",
                "authors": "Bergamini, Trifone",
                "publisher": "Zanichelli",
                "subject": "Matematica",
                "prezzo": "28,50",
                "toBuy": True,
            },
            {
                "isbn": "9788808987654",
                "title": "Fisica per i licei",
                "authors": "Amaldi",
                "publisher": "Zanichelli",
                "subject": "Fisica",
                "price": 31.00,
                "consigliato": True,
            },
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        math_books = await client.get_schoolbooks(subject_filter="matematica")
        all_books = await client.get_schoolbooks()

    assert len(math_books) == 1
    assert math_books[0].title == "Matematica Blu"
    assert math_books[0].price == 28.50
    assert math_books[0].to_buy is True

    assert len(all_books) == 2
    assert all_books[0].subject == "Fisica"  # sorted by subject
    assert all_books[0].is_recommended is True
    assert all_books[0].price == 31.00


@pytest.mark.asyncio
async def test_attachment_download_404_auto_unlock(tmp_path: Path) -> None:
    attempts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(f"{request.method} {request.url.path}")
        if "/noticeboard/attach/" in request.url.path:
            if len([a for a in attempts if "/noticeboard/attach/" in a]) == 1:
                # First attempt returns 404
                return httpx.Response(404, text="Attachment locked")
            # Second attempt returns 200
            return httpx.Response(200, content=b"UNLOCKED ATTACHMENT")
        if "/noticeboard/read/" in request.url.path:
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"Unexpected: {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        content = await client._download_attachment(
            event_code="CF", pub_id="100", num=1, auto_unlock=True
        )

    assert content == b"UNLOCKED ATTACHMENT"
    # Verify sequence: attach (404) -> read (POST unlock) -> attach (retry 200)
    assert any("/noticeboard/read/" in a for a in attempts)


# ============================================================================
# 6. Stress Tests: Malformed payloads, boundary conditions & edge cases
# ============================================================================

@pytest.mark.asyncio
async def test_didactics_malformed_and_extreme_edge_cases(tmp_path: Path) -> None:
    payload = {
        "didactics": [
            None,  # non-dict item in items list
            "not_a_dict",
            12345,
            {
                "folderId": 999,
                "folderName": "Cartella con elementi anomali",
                "folders": [
                    None,
                    "invalid",
                    {
                        "folderId": 1000,
                        "folderName": "Subfolder",
                        "contents": [
                            None,
                            42,
                            {
                                "contentId": 5002,
                                "contentName": "test_types.pdf",
                                "sizeBytes": "1048576",  # string number
                            },
                            {
                                "contentId": 5003,
                                "contentName": "test_invalid_size.pdf",
                                "sizeBytes": "NOT_A_NUMBER",  # non-numeric string
                            },
                        ],
                    },
                ],
            },
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        folders = await client.get_didactics()

    assert len(folders) == 1
    f = folders[0]
    assert f.folder_id == 1000
    assert len(f.files) == 2
    assert f.files[0].size_bytes == 1048576
    assert f.files[1].size_bytes is None


@pytest.mark.asyncio
async def test_content_disposition_rfc5987_and_various_formats(tmp_path: Path) -> None:
    cases = [
        ("attachment; filename*=UTF-8''Corso%20Python%202026.pdf", "Corso Python 2026.pdf"),
        ('attachment; filename="Esercizi_Cap_1.pdf"', "Esercizi_Cap_1.pdf"),
        ("attachment; filename=Compito.pdf", "Compito.pdf"),
        ("", "didactic_777"),  # fallback
    ]

    for cd_header, expected_fname in cases:
        async def handler(request: httpx.Request, header_val: str = cd_header) -> httpx.Response:
            headers = {"content-type": "application/pdf"}
            if header_val:
                headers["content-disposition"] = header_val
            return httpx.Response(200, content=b"%PDF test", headers=headers)

        client, http_client = _client(tmp_path, handler)
        async with http_client:
            _, fname, _ = await client.download_didactics_file(777)
        assert fname == expected_fname


@pytest.mark.asyncio
async def test_calendar_dow_and_date_edge_cases(tmp_path: Path) -> None:
    payload = {
        "calendar": [
            {"dayDate": "20260901", "dayOfWeek": "2", "status": "wd"},  # lowercase status & string dow
            {"dayDate": "invalid_date", "status": "WD"},  # invalid date (should be skipped or sliced)
            {"date": "", "status": "WD"},  # empty date (must be skipped)
            {"dayDate": "20261101", "dow": "not_an_int", "dayStatus": "hd"},  # non-numeric dow
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        days = await client.get_calendar()

    # Valid days: 2026-09-01, 2026-11-01 (and invalid_date truncated to 10 chars "invalid_da")
    dates = [d.date for d in days]
    assert "2026-09-01" in dates
    assert "2026-11-01" in dates

    sep1 = next(d for d in days if d.date == "2026-09-01")
    assert sep1.status == "WD"
    assert sep1.day_of_week == 2
    assert sep1.is_holiday is False

    nov1 = next(d for d in days if d.date == "2026-11-01")
    assert nov1.status == "HD"
    assert nov1.day_of_week == 1  # defaulted safely
    assert nov1.is_holiday is True


@pytest.mark.asyncio
async def test_get_notes_raw_list_and_period_filter(tmp_path: Path) -> None:
    # Test payload given as direct list and filtering by period
    payload = [
        {
            "evtId": 8001,
            "evtCode": "NTTE",
            "evtDate": "2026-10-10",
            "period": "Q1",
            "evtText": "Nota Q1",
        },
        {
            "evtId": 8002,
            "evtCode": "NTCL",
            "evtDate": "2026-03-15",
            "period": "Q2",
            "evtText": "Nota Q2",
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        q1_notes = await client.get_notes(period_filter="q1")
        all_notes = await client.get_notes()

    assert len(q1_notes) == 1
    assert q1_notes[0].id == "8001"
    assert q1_notes[0].text == "Nota Q1"

    assert len(all_notes) == 2


@pytest.mark.asyncio
async def test_download_document_content_length_limit(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"too large header",
            headers={"content-length": str(MAX_ATTACHMENT_BYTES + 1024)},
        )

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="Allegato troppo grande"):
            await client.download_document("hash_huge")


@pytest.mark.asyncio
async def test_download_document_streaming_byte_limit(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 5000)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="Allegato troppo grande"):
            await client.download_document("hash_stream", max_bytes=2500)


@pytest.mark.asyncio
async def test_concurrent_client_calls(tmp_path: Path) -> None:
    # Verify concurrency does not break client or semaphore
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/didactics" in request.url.path:
            return httpx.Response(200, json={"didactics": []})
        if "/calendar" in request.url.path:
            return httpx.Response(200, json={"calendar": []})
        if "/notes" in request.url.path:
            return httpx.Response(200, json={"notes": []})
        return httpx.Response(200, json={})

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        tasks = [
            client.get_didactics(),
            client.get_calendar(),
            client.get_notes(),
            client.get_didactics(),
            client.get_calendar(),
            client.get_notes(),
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 6
