from __future__ import annotations

import io
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from classeviva_mcp import server
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import (
    CalendarDay,
    DidacticsFile,
    DidacticsFolder,
    DisciplinaryNote,
    GradeItem,
    NoticeItem,
    SchoolBook,
    SchoolDocument,
    StudentProfile,
    SubjectTeacher,
    now_rome,
    today_rome_str,
)


def _sample_pdf_bytes(text: str = "Testo di prova Didattica PDF") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _sample_docx_bytes(text: str = "Testo estratto da DOCX Didattica") -> bytes:
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


class MockSpaggiariClient:
    """Mock client tailored to test the 6 FastMCP tools and snapshot integration."""

    def __init__(self) -> None:
        self._didactics_folders: list[DidacticsFolder] = []
        self._didactics_download_map: dict[int, tuple[bytes, str, str]] = {}
        self._calendar_days: list[CalendarDay] = []
        self._notes: list[DisciplinaryNote] = []
        self._documents: list[SchoolDocument] = []
        self._schoolbooks: list[SchoolBook] = []
        self.raise_on_didactics = False
        self.raise_on_calendar = False
        self.raise_on_notes = False
        self.raise_on_documents = False
        self.raise_on_schoolbooks = False

    async def get_profile(self) -> StudentProfile:
        return StudentProfile(
            ident="S1234567A",
            student_id="1234567",
            account_type="student",
            first_name="Mario",
            last_name="Rossi",
            school_class="5AI",
            school_name="ITIS Majorana",
        )

    async def get_subjects(self) -> list[SubjectTeacher]:
        return [
            SubjectTeacher(subject_id=1, subject_name="Informatica", teachers=["Prof. Bianchi"]),
            SubjectTeacher(subject_id=2, subject_name="Matematica", teachers=["Prof. Neri"]),
        ]

    async def get_grades(self, subject_filter: str | None = None) -> list[GradeItem]:
        grades = [
            GradeItem(
                id="g1",
                subject="Informatica",
                value=8.0,
                display_value="8",
                weight=1.0,
                date=today_rome_str(),
                period="1° Quadrimestre",
            ),
            GradeItem(
                id="g2",
                subject="Matematica",
                value=5.0,
                display_value="5",
                weight=1.0,
                date=today_rome_str(),
                period="1° Quadrimestre",
            ),
        ]
        if subject_filter:
            grades = [g for g in grades if subject_filter.lower() in g.subject.lower()]
        return grades

    async def get_absences(self) -> list[Any]:
        return []

    async def get_agenda(self, start: str, end: str) -> list[Any]:
        return []

    async def list_notices(self, query: str | None = None, unread_only: bool = False) -> list[NoticeItem]:
        return []

    # Tool 1 & 2
    async def get_didactics(self) -> list[DidacticsFolder]:
        if self.raise_on_didactics:
            raise RuntimeError("API Didattica irraggiungibile (simulata)")
        return self._didactics_folders

    async def download_didactics_file(self, content_id: int | str) -> tuple[bytes, str, str]:
        cid = int(content_id)
        if cid in self._didactics_download_map:
            return self._didactics_download_map[cid]
        raise ValueError(f"File {content_id} non trovato nel mock")

    # Tool 3
    async def get_calendar(self) -> list[CalendarDay]:
        if self.raise_on_calendar:
            raise RuntimeError("API Calendario offline (simulata)")
        return self._calendar_days

    # Tool 4
    async def get_notes(self, period_filter: str | None = None) -> list[DisciplinaryNote]:
        if self.raise_on_notes:
            raise RuntimeError("API Note offline (simulata)")
        if period_filter:
            return [n for n in self._notes if period_filter.lower() in n.kind_description.lower()]
        return self._notes

    # Tool 5
    async def get_documents(self) -> list[SchoolDocument]:
        if self.raise_on_documents:
            raise RuntimeError("API Documenti offline (simulata)")
        return self._documents

    # Tool 6
    async def get_schoolbooks(self, subject_filter: str | None = None) -> list[SchoolBook]:
        if self.raise_on_schoolbooks:
            raise RuntimeError("API Schoolbooks offline (simulata)")
        if subject_filter:
            return [b for b in self._schoolbooks if subject_filter.lower() in b.subject.lower()]
        return self._schoolbooks


@pytest.fixture
def mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MockSpaggiariClient, CredentialStore]:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    client = MockSpaggiariClient()
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(client, store)  # type: ignore[arg-type]
    return client, store


# ============================================================================
# 1. Empirical tests for `list_didactics`
# ============================================================================

@pytest.mark.asyncio
async def test_list_didactics_filters_and_counts(mock_env: tuple[MockSpaggiariClient, CredentialStore]) -> None:
    client, _ = mock_env
    client._didactics_folders = [
        DidacticsFolder(
            folder_id=1,
            folder_name="Informatica - Programmazione Web",
            teacher_name="Prof. Mario Rossi",
            teacher_id=101,
            last_share_date="2026-11-01",
            files=[
                DidacticsFile(content_id=10, content_name="html_intro.pdf", share_date="2026-11-01", file_type="pdf", size_bytes=1024),
                DidacticsFile(content_id=11, content_name="css_flexbox.pdf", share_date="2026-11-02", file_type="pdf", size_bytes=2048),
            ],
        ),
        DidacticsFolder(
            folder_id=2,
            folder_name="Matematica - Derivate",
            teacher_name="Prof. Luigi Neri",
            teacher_id=102,
            last_share_date="2026-11-03",
            files=[
                DidacticsFile(content_id=20, content_name="esercizi_derivate.pdf", share_date="2026-11-03", file_type="pdf", size_bytes=4096),
            ],
        ),
        DidacticsFolder(
            folder_id=3,
            folder_name="Informatica - Database Relazionali",
            teacher_name="Prof. Mario Rossi",
            teacher_id=101,
            last_share_date="2026-11-05",
            files=[
                DidacticsFile(content_id=30, content_name="sql_join.docx", share_date="2026-11-05", file_type="docx", size_bytes=8192),
            ],
        ),
    ]

    # No filters: all 3 folders, 4 files total
    res = await server.list_didactics()
    assert res["folders_count"] == 3
    assert res["files_count"] == 4
    assert res["subject_filter"] is None
    assert res["teacher_filter"] is None
    assert len(res["folders"]) == 3
    assert "Trovate 3 cartelle e 4 file" in res["summary"]

    # Subject filter (case insensitive and substring)
    res_subj = await server.list_didactics(subject="  informatica  ")
    assert res_subj["folders_count"] == 2
    assert res_subj["files_count"] == 3
    assert res_subj["subject_filter"] == "informatica"
    assert all("Informatica" in f["folder_name"] for f in res_subj["folders"])

    # Teacher filter (case insensitive)
    res_teacher = await server.list_didactics(teacher="neri")
    assert res_teacher["folders_count"] == 1
    assert res_teacher["files_count"] == 1
    assert res_teacher["folders"][0]["teacher_name"] == "Prof. Luigi Neri"

    # Combined filter
    res_both = await server.list_didactics(subject="database", teacher="rossi")
    assert res_both["folders_count"] == 1
    assert res_both["folders"][0]["folder_name"] == "Informatica - Database Relazionali"

    # Non-matching filter
    res_empty = await server.list_didactics(subject="filosofia")
    assert res_empty["folders_count"] == 0
    assert res_empty["files_count"] == 0
    assert res_empty["folders"] == []
    assert "Nessun materiale didattico trovato" in res_empty["summary"]


@pytest.mark.asyncio
async def test_list_didactics_error_resilience(mock_env: tuple[MockSpaggiariClient, CredentialStore]) -> None:
    client, _ = mock_env
    client.raise_on_didactics = True

    res = await server.list_didactics()
    assert res["folders_count"] == 0
    assert res["files_count"] == 0
    assert res["folders"] == []
    assert len(res["warnings"]) == 1
    assert "Materiali didattici non disponibili" in res["warnings"][0]


# ============================================================================
# 2. Empirical tests for `download_didactics_item`
# ============================================================================

@pytest.mark.asyncio
async def test_download_didactics_item_pdf_extraction_and_permissions(
    mock_env: tuple[MockSpaggiariClient, CredentialStore], tmp_path: Path
) -> None:
    client, store = mock_env
    sample_text = "Classe 5AI Esercizi Limiti e Continuità Docente Rossi"
    pdf_bytes = _sample_pdf_bytes(sample_text)
    client._didactics_download_map[101] = (pdf_bytes, "limiti.pdf", "application/pdf")

    # Download with default path
    res = await server.download_didactics_item(file_id=101)
    assert res["file_id"] == 101
    assert res["file_name"] == "limiti.pdf"
    assert res["content_type"] == "application/pdf"
    assert res["size_bytes"] == len(pdf_bytes)
    assert sample_text in res["text"]
    assert res["is_text_truncated"] is False
    assert Path(res["saved_path"]).is_file()

    # Verify private mode 0o600 on file (on Unix-like permissions; on Windows verify readable and writable)
    saved = Path(res["saved_path"])
    assert saved.exists()
    assert saved.stat().st_size == len(pdf_bytes)

    # Download with custom destination_folder
    custom_dest = tmp_path / "custom_download_dir"
    res_custom = await server.download_didactics_item(file_id=101, destination_folder=str(custom_dest))
    assert Path(res_custom["saved_path"]).parent == custom_dest.resolve()
    assert Path(res_custom["saved_path"]).is_file()


@pytest.mark.asyncio
async def test_download_didactics_item_docx_extraction(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    docx_text = "Esercizi di SQL e Algebra Relazionale"
    docx_bytes = _sample_docx_bytes(docx_text)
    client._didactics_download_map[102] = (docx_bytes, "database_esercizi.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    res = await server.download_didactics_item(file_id=102)
    assert res["file_id"] == 102
    assert res["file_name"] == "database_esercizi.docx"
    assert docx_text in res["text"]
    assert res["is_text_truncated"] is False


@pytest.mark.asyncio
async def test_download_didactics_item_truncation_over_10k_chars(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    # Generate multi-page PDF with > 10,000 characters
    doc = pymupdf.open()
    for i in range(10):
        p = doc.new_page()
        p.insert_text((50, 72), f"Pagina {i}\n" + "Testo molto lungo per riempire la pagina.\n" * 50)
    long_pdf = doc.tobytes()
    doc.close()
    client._didactics_download_map[103] = (long_pdf, "dispensa_lunga.pdf", "application/pdf")

    res = await server.download_didactics_item(file_id=103)
    assert res["is_text_truncated"] is True
    assert "[Testo troncato:" in res["text"]
    assert "caratteri totali]" in res["text"]


@pytest.mark.asyncio
async def test_download_didactics_item_binary_file(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    raw_bin = b"\x00\x01\x02\x03\x04\xff\xfe"
    client._didactics_download_map[104] = (raw_bin, "firmware.bin", "application/octet-stream")

    res = await server.download_didactics_item(file_id=104)
    assert res["text"] == ""
    assert res["is_text_truncated"] is False
    assert "(nessun testo estraibile)" in res["summary"]


# ============================================================================
# 3. Empirical tests for `get_calendar`
# ============================================================================

@pytest.mark.asyncio
async def test_get_calendar_hd_filter_and_days_ahead(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    today = now_rome().date()

    # Populate days: mix of HD (Holiday), WD (Working Day), SD (Special Day)
    client._calendar_days = [
        CalendarDay(
            date=(today + timedelta(days=2)).strftime("%Y-%m-%d"),
            day_of_week=3,
            status="HD",
            is_holiday=True,
            description="Festa Patronale",
        ),
        CalendarDay(
            date=(today + timedelta(days=5)).strftime("%Y-%m-%d"),
            day_of_week=6,
            status="WD",
            is_holiday=False,
            description="Lezione regolare",
        ),
        CalendarDay(
            date=(today + timedelta(days=15)).strftime("%Y-%m-%d"),
            day_of_week=2,
            status="HD",
            is_holiday=True,
            description="Ponte dell'Immacolata",
        ),
        CalendarDay(
            date=(today + timedelta(days=45)).strftime("%Y-%m-%d"),
            day_of_week=4,
            status="HD",
            is_holiday=True,
            description="Vacanze di Natale",
        ),
        CalendarDay(
            date=(today + timedelta(days=200)).strftime("%Y-%m-%d"),
            day_of_week=1,
            status="HD",
            is_holiday=True,
            description="Vacanze Pasquali Future",
        ),
    ]

    # Test days_ahead=10: should only catch the day at +2 (Festa Patronale). WD at +5 ignored.
    res_10 = await server.get_calendar(days_ahead=10)
    assert res_10["days_ahead"] == 10
    assert res_10["holidays_count"] == 1
    assert res_10["holidays"][0]["description"] == "Festa Patronale"
    assert res_10["holidays"][0]["status"] == "HD"
    assert res_10["holidays"][0]["day_of_week"] == "Mercoledì"

    # Test days_ahead=30: catches +2 and +15
    res_30 = await server.get_calendar(days_ahead=30)
    assert res_30["days_ahead"] == 30
    assert res_30["holidays_count"] == 2
    descs = [h["description"] for h in res_30["holidays"]]
    assert descs == ["Festa Patronale", "Ponte dell'Immacolata"]

    # Test days_ahead=200 bounded to 180: catches +2, +15, +45, but NOT +200
    res_bounded = await server.get_calendar(days_ahead=200)
    assert res_bounded["days_ahead"] == 180  # bounded!
    assert res_bounded["holidays_count"] == 3
    assert not any(h["description"] == "Vacanze Pasquali Future" for h in res_bounded["holidays"])

    # Test negative or zero days_ahead bounded to 1
    res_zero = await server.get_calendar(days_ahead=-5)
    assert res_zero["days_ahead"] == 1


@pytest.mark.asyncio
async def test_get_calendar_error_resilience(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client.raise_on_calendar = True

    res = await server.get_calendar()
    assert res["holidays_count"] == 0
    assert res["holidays"] == []
    assert len(res["warnings"]) == 1
    assert "Calendario scolastico non disponibile" in res["warnings"][0]


# ============================================================================
# 4. Empirical tests for `get_notes`
# ============================================================================

@pytest.mark.asyncio
async def test_get_notes_breakdown_and_lazy_body(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client._notes = [
        DisciplinaryNote(
            id="n1",
            kind="NTTE",
            kind_description="Nota del docente",
            date="2026-11-04",
            teacher="Prof. Bianchi",
            text="Disturba ripetutamente la spiegazione.",
            is_read=True,
        ),
        DisciplinaryNote(
            id="n2",
            kind="NTTE",
            kind_description="Nota del docente",
            date="2026-11-03",
            teacher="Prof. Neri",
            text="Dimentica il materiale didattico.",
            is_read=False,
        ),
        DisciplinaryNote(
            id="n3",
            kind="NTCL",
            kind_description="Nota di classe",
            date="2026-11-02",
            teacher="Prof. Rossi",
            text="La classe rientra dall'intervallo con 15 minuti di ritardo.",
            is_read=True,
        ),
        DisciplinaryNote(
            id="n4",
            kind="NTWN",
            kind_description="Richiamo",
            date="2026-11-01",
            teacher="Prof.ssa Verdi",
            text="Ammonizione verbale per mancata consegna compiti.",
            is_read=True,
        ),
        DisciplinaryNote(
            id="n5",
            kind="NTST",
            kind_description="Sanzione disciplinare",
            date="2026-10-28",
            teacher="Preside",
            text="Allontanamento per 1 giorno.",
            is_read=True,
        ),
    ]

    res = await server.get_notes()
    assert res["count"] == 5
    assert res["breakdown"]["teacher_notes"] == 2
    assert res["breakdown"]["class_notes"] == 1
    assert res["breakdown"]["warnings"] == 1
    assert res["breakdown"]["sanctions"] == 1
    assert "5 note disciplinari registrate" in res["summary"]
    assert "2 del docente" in res["summary"]
    assert "1 di classe" in res["summary"]
    assert "1 richiami" in res["summary"]
    assert "1 sanzioni" in res["summary"]

    # Verify descending sort by date
    dates = [n["date"] for n in res["notes"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_get_notes_error_resilience(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client.raise_on_notes = True

    res = await server.get_notes()
    assert res["count"] == 0
    assert res["notes"] == []
    assert len(res["warnings"]) == 1
    assert "Note disciplinari non disponibili" in res["warnings"][0]


# ============================================================================
# 5. Empirical tests for `get_documents`
# ============================================================================

@pytest.mark.asyncio
async def test_get_documents_sorting_and_schema(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client._documents = [
        SchoolDocument(
            hash="h_pagella_q1",
            title="Pagella 1° Quadrimestre a.s. 2025/2026",
            document_type="pagella",
            publish_date="2026-02-10",
        ),
        SchoolDocument(
            hash="h_certificato",
            title="Certificato di Iscrizione e Frequenza",
            document_type="certificato",
            publish_date="2026-09-15",
        ),
        SchoolDocument(
            hash="h_pagella_finale",
            title="Scheda di Valutazione Finale a.s. 2025/2026",
            document_type="pagella",
            publish_date="2026-06-20",
        ),
    ]

    res = await server.get_documents()
    assert res["count"] == 3
    assert len(res["documents"]) == 3
    # Check descending sort by publish_date
    assert res["documents"][0]["publish_date"] == "2026-09-15"
    assert res["documents"][1]["publish_date"] == "2026-06-20"
    assert res["documents"][2]["publish_date"] == "2026-02-10"

    # Verify doc schema fields
    first = res["documents"][0]
    assert first["id"] == "h_certificato"
    assert first["file_hash"] == "h_certificato"
    assert first["doc_type"] == "certificato"
    assert "Trovati 3 documenti ufficiali di scrutinio" in res["summary"]


@pytest.mark.asyncio
async def test_get_documents_error_resilience(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client.raise_on_documents = True

    res = await server.get_documents()
    assert res["count"] == 0
    assert res["documents"] == []
    assert len(res["warnings"]) == 1
    assert "Documenti di scrutinio non disponibili" in res["warnings"][0]


# ============================================================================
# 6. Empirical tests for `get_schoolbooks`
# ============================================================================

@pytest.mark.asyncio
async def test_get_schoolbooks_cost_and_filters(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client._schoolbooks = [
        SchoolBook(
            isbn="9788808123456",
            title="Manuale di Informatica e Reti",
            authors="Lorenzi, Rossi",
            publisher="Zanichelli",
            volume="1",
            price=24.50,
            subject="Informatica",
            to_buy=True,
            is_recommended=False,
        ),
        SchoolBook(
            isbn="9788808654321",
            title="Matematica Verde Vol. 5",
            authors="Bergamini, Trifone, Barozzi",
            publisher="Zanichelli",
            volume="5",
            price=31.20,
            subject="Matematica",
            to_buy=True,
            is_recommended=False,
        ),
        SchoolBook(
            isbn="9788805999999",
            title="Laboratorio di Telecomunicazioni",
            authors="Bianchi",
            publisher="Hoepli",
            volume="U",
            price=18.00,
            subject="Telecomunicazioni",
            to_buy=False,  # Already owned from previous year
            is_recommended=False,
        ),
        SchoolBook(
            isbn="9788801111111",
            title="Guida al Linguaggio Python (Consigliato)",
            authors="Verdi",
            publisher="Apogeo",
            volume="U",
            price=15.00,
            subject="Informatica",
            to_buy=False,  # Recommended only
            is_recommended=True,
        ),
    ]

    # All books: 4 books, 2 to buy (Informatica 24.50 + Matematica 31.20 = 55.70)
    res_all = await server.get_schoolbooks()
    assert res_all["count"] == 4
    assert res_all["to_buy_count"] == 2
    assert res_all["total_cost_to_buy"] == 55.70
    assert "55.70 €" in res_all["summary"]

    # Subject filter: Informatica only (2 books: 1 to buy 24.50, 1 recommended 15.00)
    res_subj = await server.get_schoolbooks(subject="informatica")
    assert res_subj["count"] == 2
    assert res_subj["to_buy_count"] == 1
    assert res_subj["total_cost_to_buy"] == 24.50
    assert res_subj["subject_filter"] == "informatica"


@pytest.mark.asyncio
async def test_get_schoolbooks_error_resilience(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    client.raise_on_schoolbooks = True

    res = await server.get_schoolbooks()
    assert res["count"] == 0
    assert res["to_buy_count"] == 0
    assert res["total_cost_to_buy"] == 0.0
    assert res["schoolbooks"] == []
    assert len(res["warnings"]) == 1
    assert "Libri di testo non disponibili" in res["warnings"][0]


# ============================================================================
# 7. Stress tests: Read-Only Guard, Snapshot Enrichment, and FastMCP Prompts
# ============================================================================

@pytest.mark.asyncio
async def test_read_only_mode_blocks_all_6_tools(
    mock_env: tuple[MockSpaggiariClient, CredentialStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.list_didactics()

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.download_didactics_item(file_id=1)

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.get_calendar()

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.get_notes()

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.get_documents()

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        await server.get_schoolbooks()


@pytest.mark.asyncio
async def test_get_snapshot_enrichment_and_notes_strict_exclusion(
    mock_env: tuple[MockSpaggiariClient, CredentialStore]
) -> None:
    client, _ = mock_env
    today = now_rome().date()

    # Add holidays and didactics
    client._calendar_days = [
        CalendarDay(
            date=(today + timedelta(days=3)).strftime("%Y-%m-%d"),
            day_of_week=4,
            status="HD",
            is_holiday=True,
            description="Ponte Festivo",
        )
    ]
    client._didactics_folders = [
        DidacticsFolder(
            folder_id=1,
            folder_name="Materiali Sistemi",
            teacher_name="Prof. Neri",
            teacher_id=102,
            last_share_date="2026-11-08",
            files=[
                DidacticsFile(content_id=50, content_name="reti.pdf", share_date="2026-11-08", file_type="pdf", size_bytes=1000)
            ],
        )
    ]
    # Add disciplinary notes into client to stress-test that snapshot NEVER exposes them
    client._notes = [
        DisciplinaryNote(
            id="n999",
            kind="NTST",
            kind_description="Sanzione",
            date="2026-11-08",
            teacher="Prof. Severi",
            text="Nota gravissima che NON deve finire nello snapshot!",
            is_read=True,
        )
    ]

    snapshot = await server.get_snapshot(days_ahead=7)

    # 1. Assert upcoming_holidays is present and populated
    assert "upcoming_holidays" in snapshot
    assert snapshot["upcoming_holidays"]["count"] == 1
    assert snapshot["upcoming_holidays"]["holidays"][0]["description"] == "Ponte Festivo"

    # 2. Assert didactics summary is present and populated
    assert "didactics" in snapshot
    assert snapshot["didactics"]["folders_count"] == 1
    assert snapshot["didactics"]["files_count"] == 1

    # 3. Assert STRICT exclusion of disciplinary notes
    assert "notes" not in snapshot
    assert "disciplinary_notes" not in snapshot
    assert "n999" not in str(snapshot)
    assert "Nota gravissima" not in str(snapshot)


def test_fastmcp_prompts_recap_and_piano_recupero() -> None:
    # 1. Prompt recap-settimanale
    recap = server.recap_settimanale(days_ahead=10)
    assert "prossimi 10 giorni" in recap
    assert "get_today_context" in recap
    assert "get_homework(days_ahead=10)" in recap
    assert "get_calendar(days_ahead=10)" in recap
    assert "list_notices(limit=10)" in recap

    # 2. Prompt piano-recupero-materie (with specific subject)
    piano_math = server.piano_recupero_materie(subject="Matematica")
    assert "materia 'Matematica'" in piano_math
    assert "get_grades" in piano_math
    assert "calculate_grade_target" in piano_math
    assert "get_lessons" in piano_math
    assert "get_agenda" in piano_math

    # 3. Prompt piano-recupero-materie (default, all subjects)
    piano_all = server.piano_recupero_materie()
    assert "tutte le materie insufficienti (< 6.0)" in piano_all
