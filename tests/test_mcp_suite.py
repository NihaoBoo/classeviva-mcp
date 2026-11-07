from __future__ import annotations

import io
from pathlib import Path

import httpx
import openpyxl
import pymupdf
import pytest

from classeviva_mcp.client import DEFAULT_HEADERS, SpaggiariClient
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.pdf_utils import build_class_regex, extract_pdf_text, extract_xlsx_text
from classeviva_mcp.server import (
    calculate_grade_target,
    daily_brief,
    get_absences,
    get_agenda,
    get_grades,
    get_homework,
    get_lessons,
    get_today_context,
    list_notices,
    read_notice_pdf,
    recovery_plan,
    save_class_timetable,
    set_client_for_testing,
    setup,
    update_student_context,
)


def _make_sample_pdf(pages_text: list[str]) -> bytes:
    doc = pymupdf.open()
    for txt in pages_text:
        page = doc.new_page()
        if txt:
            page.insert_text((72, 72), txt)
    return doc.tobytes()


def _make_sample_timetable_xlsx() -> bytes:
    """Creates a realistic school timetable Excel sheet with classes on columns and merged 2-hour lab cells."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orario Classi"
    ws.append(["Giorno", "Ora", "1AC", "3AM", "5^ AI"])
    ws.append(["Lunedi", "08:00", "Italiano", "Meccanica", "Lab. Sistemi (Bianchi)"])
    ws.append(["Lunedi", "09:00", "Storia", "Matematica", None])
    # Merge the 2-hour lab in column E (row 2 to row 3, col 5)
    ws.merge_cells(start_row=2, start_column=5, end_row=3, end_column=5)
    ws.append(["Lunedi", "10:00", "Chimica", "Inglese", "Informatica (Rossi)"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_tolerant_class_regex_and_ai_aliases() -> None:
    rx = build_class_regex("5AI, Informatica")
    assert rx is not None
    for variant in ("5AI", "5 AI", "5^AI", "5ª AI", "5° AI", "V AI", "5 A I", "Indirizzo Informatica"):
        assert rx.search(variant), f"Failed to match variant: {variant}"
    assert not rx.search("Classe 3AM Meccanica")


def test_xlsx_merged_cells_and_vertical_column_extraction() -> None:
    xlsx_bytes = _make_sample_timetable_xlsx()
    extracted = extract_xlsx_text(xlsx_bytes, filter_keyword="5AI")

    # Verify vertical column extraction kept 5^ AI and dropped 1AC / 3AM
    assert "Estrazione Verticale Colonna 5AI" in extracted
    assert "Lab. Sistemi (Bianchi)" in extracted
    # Merged cell value must appear in both 08:00 and 09:00 rows!
    assert extracted.count("Lab. Sistemi (Bianchi)") == 2
    assert "Meccanica" not in extracted


def test_pdf_smart_page_filter_saves_tokens() -> None:
    pdf_bytes = _make_sample_pdf(
        [
            "Circolare n. 90 - Orario provvisorio generale di tutte le classi.",
            "Pagina 2: Orario classi 1AC, 2AC, 3AC Chimica",
            "Pagina 3: Orario classi 1AM, 2AM, 3AM Meccanica",
            "Pagina 4: Orario classe 5^ AI (Informatica) - Aula 12",
            "Pagina 5: Orario classi 1AL, 2AL Logistica",
        ]
    )
    txt, total_pages, included = extract_pdf_text(pdf_bytes, filter_keyword="5AI")
    assert total_pages == 5
    assert included == [1, 4]
    assert "Aula 12" in txt
    assert "Meccanica" not in txt


@pytest.mark.asyncio
async def test_email_login_with_parent_and_student_choices_and_401_reauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLASSEVIVA_USER", "genitore.personal@gmail.com")
    monkeypatch.setenv("CLASSEVIVA_PASSWORD", "SecretPass123")
    monkeypatch.setenv("CLASSROOM_ICAL_URL", "")

    store = CredentialStore(fallback_dir=tmp_path)
    login_payloads: list[dict] = []

    async def mock_spaggiari(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Z-Dev-Apikey") == DEFAULT_HEADERS["Z-Dev-Apikey"]
        assert request.headers.get("User-Agent") == DEFAULT_HEADERS["User-Agent"]

        path = request.url.path
        if path.endswith("/auth/login"):
            import json
            body = json.loads(request.content.decode("utf-8"))
            login_payloads.append(body)
            if body.get("ident") is None:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"ident": "G9876543X", "firstName": "MARIO", "lastName": "ROSSI"},
                            {"ident": "S9876543X", "firstName": "MARIO", "lastName": "ROSSI"},
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ident": body["ident"],
                    "firstName": "MARIO",
                    "lastName": "ROSSI",
                    "token": "valid-jwt-token-2026",
                },
            )

        if path.endswith("/cards") or path.endswith("/card"):
            return httpx.Response(
                200,
                json={
                    "cards": [
                        {
                            "usrId": 9876543,
                            "firstName": "MARIO",
                            "lastName": "ROSSI",
                            "classDesc": "5AI INFORMATICA",
                            "schName": "ITIS GALILEI",
                        }
                    ]
                },
            )

        return httpx.Response(200, json={})

    transport = httpx.MockTransport(mock_spaggiari)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = SpaggiariClient(store=store, http_client=http_client)
        profile = await client.get_profile()

    assert len(login_payloads) == 2
    assert profile.student_id == "9876543"
    assert profile.account_type == "parent"
    assert profile.school_class == "5AI INFORMATICA"


@pytest.mark.asyncio
async def test_all_mcp_tools_student_context_scanned_pdf_and_zero_stdout_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLASSEVIVA_USER", "studente@gmail.com")
    monkeypatch.setenv("CLASSEVIVA_PASSWORD", "SecretPass123")
    monkeypatch.setenv("CLASSROOM_ICAL_URL", "")

    # Notice 245: normal text PDF; Notice 246: scanned image PDF (0 text)
    sample_pdf_bytes = _make_sample_pdf(
        ["Circolare n. 245 - Gita Scolastica 5AI a Barcellona. Consegnare modulo firmato entro il 20/11/2026."]
    )
    scanned_pdf_bytes = _make_sample_pdf([""])

    async def mock_spaggiari(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/login"):
            return httpx.Response(
                200,
                json={"ident": "S1234567X", "firstName": "LUCA", "lastName": "VERDI", "token": "tok-123"},
            )
        if path.endswith("/cards") or path.endswith("/card"):
            return httpx.Response(
                200,
                json={"cards": [{"usrId": 1234567, "classDesc": "", "schName": "ITIS GALILEI"}]},
            )
        if path.endswith("/subjects"):
            return httpx.Response(
                200,
                json={
                    "subjects": [
                        {"id": 1, "description": "INFORMATICA", "teachers": [{"teacherName": "ROSSI MARIO"}]}
                    ]
                },
            )
        if "/agenda/all/" in path:
            return httpx.Response(
                200,
                json={
                    "agenda": [
                        {
                            "evtId": 10,
                            "evtDatetimeBegin": "2026-11-09T08:00:00+01:00",
                            "subjectDesc": "SISTEMI E RETI",
                            "notes": "Verifica scritta su VLAN e Subnetting",
                            "evtCode": "AGNT",
                            "authorName": "BIANCHI",
                        }
                    ]
                },
            )
        if "/lessons/" in path:
            return httpx.Response(
                200,
                json={
                    "lessons": [
                        {
                            "evtId": 99,
                            "evtDate": "2026-11-04",
                            "subjectDesc": "SISTEMI E RETI",
                            "lessonType": "Lezione",
                            "lessonArg": "Configurazione trunk 802.1Q e inter-VLAN routing",
                            "authorName": "BIANCHI",
                            "evtHPos": 2,
                        }
                    ]
                },
            )
        if path.endswith("/grades"):
            return httpx.Response(
                200,
                json={
                    "grades": [
                        {
                            "evtId": 501,
                            "subjectDesc": "SISTEMI E RETI",
                            "decimalValue": 4.5,
                            "displayValue": "4½",
                            "weightFactor": 1.0,
                            "evtDate": "2026-10-25",
                            "periodDesc": "Trimestre",
                            "componentDesc": "Scritto",
                        }
                    ]
                },
            )
        if path.endswith("/absences/details"):
            return httpx.Response(
                200,
                json={"events": [{"evtId": 701, "evtDate": "2026-10-15", "evtCode": "ABA0", "isJustified": False}]},
            )
        if path.endswith("/noticeboard"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "pubId": 245,
                            "evtCode": "CF",
                            "cntTitle": "Circolare Gita 5AI Barcellona",
                            "pubDT": "2026-11-06T10:00:00",
                            "cntCategory": "Visite e viaggi",
                            "readStatus": True,
                            "attachments": [{"attachNum": 1, "fileName": "gita_5ai.pdf"}],
                        },
                        {
                            "pubId": 246,
                            "evtCode": "CF",
                            "cntTitle": "Circolare Scansionata",
                            "pubDT": "2026-11-06T11:00:00",
                            "readStatus": True,
                            "attachments": [{"attachNum": 1, "fileName": "scan.pdf"}],
                        },
                    ]
                },
            )
        if "/noticeboard/read/" in path:
            return httpx.Response(200, json={"item": {"cntText": ""}})
        if "/noticeboard/attach/CF/245/" in path:
            return httpx.Response(200, content=sample_pdf_bytes)
        if "/noticeboard/attach/CF/246/" in path:
            return httpx.Response(200, content=scanned_pdf_bytes)

        return httpx.Response(200, json={})

    store = CredentialStore(fallback_dir=tmp_path)
    transport = httpx.MockTransport(mock_spaggiari)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = SpaggiariClient(store=store, http_client=http_client)
        set_client_for_testing(client, store)

        # Save persistent student context & timetable. The iCal URL is no longer accepted
        # by the MCP tool: it must be configured via CLI and never pass through the LLM.
        await update_student_context(
            school_class="5AI",
            course_name="Informatica",
            register_number=14,
            custom_notes="5AI = Informatica, 5AC = Chimica, 5AM = Meccanica, 5AL = Logistica",
        )
        assert not hasattr(store.load_student_context(), "classroom_ical_url")

        tt_res = await save_class_timetable(
            school_class="5AI",
            schedule={"Lunedi": [{"hour": 1, "start_time": "08:00", "subject": "Informatica", "room": "Lab 2"}]},
            valid_from="2026-11-01",
        )
        assert tt_res["status"] == "timetable_saved"

        ctx = await get_today_context()
        assert ctx["profile"]["school_class"] == "5AI"
        assert ctx["student_context"]["register_number"] == 14
        assert "school_year_status" in ctx
        assert ctx["school_year_status"]["needs_class_review"] is False
        assert len(ctx["subjects_and_teachers"]) == 1

        # Verify bounded FIFO storage limit for seen_notice_ids (max 200)
        store.mark_notices_seen([f"id_{i}" for i in range(250)])
        assert len(store.load_student_context().seen_notice_ids) == 200


        hw = await get_homework(days_ahead=7)
        assert hw["count"] == 1

        ag = await get_agenda(kind="exam")
        assert ag["count"] == 1

        ls = await get_lessons(subject="Sistemi")
        assert ls["count"] == 1

        gr = await get_grades()
        assert len(gr["insufficiencies"]) == 1

        target = await calculate_grade_target("Sistemi", target_average=6.0)
        assert target["required_next_grade"] == 7.5

        ab = await get_absences()
        assert ab["unjustified_count"] == 1

        notices = await list_notices(query="gita")
        assert notices["count"] == 1
        assert notices["notices"][0]["status_label"] in ("🆕 NUOVA", "📌 GIÀ VISTA")
        # Calling list_notices a second time still keeps the notice visible (never hides it)
        notices_second = await list_notices(query="gita")
        assert notices_second["count"] == 1
        assert notices_second["notices"][0]["seen_by_agent"] is True
        assert notices_second["notices"][0]["status_label"] == "📌 GIÀ VISTA"


        pdf_res = await read_notice_pdf(notice_id="245", save_pdf=True)
        assert "Barcellona" in pdf_res["text"]
        assert pdf_res["filter_keyword_used"] == "5AI"

        # Verify scanned image PDF renders PNG pages for Vision fallback
        scan_res = await read_notice_pdf(notice_id="246", save_pdf=True)
        assert scan_res["is_scanned_image_pdf"] is True
        assert len(scan_res["rendered_page_images"]) == 1
        assert Path(scan_res["rendered_page_images"][0]).exists()

        assert "update_student_context" in setup()
        assert "get_homework" in daily_brief()
        assert "get_lessons" in recovery_plan("Sistemi")

    captured = capsys.readouterr()
    assert captured.out == ""
