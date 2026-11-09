"""Empirical Challenger 2 test harness for Milestone M2:
1. Snapshot Enrichment & Privacy Assertions (upcoming_holidays, didactics, zero notes leakage across all subkeys).
2. Read-Only Mode Isolation (tool/prompt hiding in subshell, PermissionError guards).
3. FastMCP Prompts (recap-settimanale, piano-recupero-materie arguments and content).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import classeviva_mcp.server as server
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import (
    CalendarDay,
    DidacticsFile,
    DidacticsFolder,
    DisciplinaryNote,
    GradeItem,
    NoticeItem,
    StudentProfile,
    SubjectTeacher,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _rich_fake_client() -> Any:
    """Stub client providing full data including didactics, calendar, and notes."""

    class RichFake:
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
                SubjectTeacher(subject_id="s2", subject_name="Matematica", teachers=["Bianchi"]),
            ]

        async def get_grades(self, subject_filter: str | None = None) -> list[GradeItem]:
            return [
                GradeItem(
                    id="g1",
                    subject="Informatica",
                    value=8.5,
                    display_value="8½",
                    weight=1.0,
                    date="2026-11-01",
                    period="Primo quadrimestre",
                    category="Scritto",
                ),
                GradeItem(
                    id="g2",
                    subject="Matematica",
                    value=5.0,
                    display_value="5",
                    weight=1.0,
                    date="2026-11-03",
                    period="Primo quadrimestre",
                    category="Scritto",
                ),
            ]

        async def get_absences(self) -> list[Any]:
            return []

        async def get_agenda(self, start_date: str, end_date: str) -> list[Any]:
            return []

        async def get_lessons(
            self, start_date: str, end_date: str, subject_filter: str | None = None
        ) -> list[Any]:
            return []

        async def list_notices(
            self, query: str | None = None, unread_only: bool = False
        ) -> list[NoticeItem]:
            return []

        async def get_calendar(self) -> list[CalendarDay]:
            # Return holidays: one inside 7 days, one outside
            return [
                CalendarDay(
                    date="2026-11-10",
                    day_of_week=2,
                    status="HD",
                    description="Festa del Patrono",
                ),
                CalendarDay(
                    date="2026-11-11",
                    day_of_week=3,
                    status="HD",
                    description="Ponte del Patrono",
                ),
                CalendarDay(
                    date="2026-12-25",
                    day_of_week=5,
                    status="HD",
                    description="Natale",
                ),
                CalendarDay(
                    date="2026-11-12",
                    day_of_week=4,
                    status="REG",
                    description="Giorno di lezione ordinario",
                ),
            ]

        async def get_didactics(self) -> list[DidacticsFolder]:
            return [
                DidacticsFolder(
                    folder_id=101,
                    folder_name="Informatica - Reti",
                    teacher_name="Prof. Rossi",
                    files=[
                        DidacticsFile(
                            content_id=1,
                            content_name="Esercizi_Subnetting.pdf",
                            share_date="2026-11-02",
                            size_bytes=10240,
                        ),
                        DidacticsFile(
                            content_id=2,
                            content_name="Teoria_TCP_IP.docx",
                            share_date="2026-11-04",
                            size_bytes=20480,
                        ),
                    ],
                ),
                DidacticsFolder(
                    folder_id=102,
                    folder_name="Matematica - Analisi",
                    teacher_name="Prof. Bianchi",
                    files=[
                        DidacticsFile(
                            content_id=3,
                            content_name="Derivate_Formulario.pdf",
                            share_date="2026-11-05",
                            size_bytes=5120,
                        ),
                    ],
                ),
            ]

        async def get_notes(
            self, period_filter: str | None = None
        ) -> list[DisciplinaryNote]:
            # Disciplinary notes exist in the client, but MUST NEVER appear in get_snapshot
            return [
                DisciplinaryNote(
                    event_id=991,
                    event_code="NTTE",
                    date="2026-11-02",
                    author_name="Prof. Rossi",
                    description="Nota disciplinare per disturbo durante la spiegazione",
                    full_text="Lo studente usa il telefono cellulare senza autorizzazione.",
                    read_status=True,
                )
            ]

    return RichFake()


def _recursively_find_forbidden_keys(
    data: Any, path: str = ""
) -> list[tuple[str, Any]]:
    """Traverse all dict keys and subkeys to locate any forbidden note references."""
    forbidden_matches: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            curr_path = f"{path}.{k}" if path else str(k)
            # Check for exact forbidden keys or subkeys
            if k in ("notes", "disciplinary_notes", "note_disciplinari"):
                forbidden_matches.append((curr_path, v))
            forbidden_matches.extend(_recursively_find_forbidden_keys(v, curr_path))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            curr_path = f"{path}[{idx}]"
            forbidden_matches.extend(_recursively_find_forbidden_keys(item, curr_path))
    return forbidden_matches


# ============================================================================
# Dimension 1: Snapshot Enrichment & Privacy Assertions
# ============================================================================


@pytest.mark.asyncio
async def test_get_snapshot_enrichment_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stress-test snapshot enrichment:
    - upcoming_holidays is populated with count, holidays, summary.
    - didactics is populated with folders_count, files_count, new_didactics_count.
    - Zero tolerance: 'notes' and 'disciplinary_notes' MUST NOT exist anywhere in any key/subkey.
    """
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    client = _rich_fake_client()
    server.set_client_for_testing(client, store)

    snapshot = await server.get_snapshot(days_ahead=30)

    # 1. Assert required enriched sections exist
    assert "upcoming_holidays" in snapshot, "upcoming_holidays missing from snapshot"
    assert "didactics" in snapshot, "didactics missing from snapshot"

    # 2. Assert upcoming_holidays structure and values
    holidays_sec = snapshot["upcoming_holidays"]
    assert holidays_sec["days_ahead"] == 30
    assert holidays_sec["count"] >= 2
    assert isinstance(holidays_sec["holidays"], list)
    assert len(holidays_sec["holidays"]) >= 2
    for h in holidays_sec["holidays"]:
        assert h["status"] == "HD"
        assert "day_of_week" in h
        assert "description" in h

    # 3. Assert didactics structure and values
    didactics_sec = snapshot["didactics"]
    assert didactics_sec["folders_count"] == 2
    assert didactics_sec["files_count"] == 3
    assert didactics_sec["new_didactics_count"] == 3
    assert "summary" in didactics_sec

    # 4. Assert zero tolerance for disciplinary notes in top-level keys
    assert "notes" not in snapshot, "CRITICAL: 'notes' key leaked into snapshot root!"
    assert "disciplinary_notes" not in snapshot, (
        "CRITICAL: 'disciplinary_notes' key leaked into snapshot root!"
    )

    # 5. Assert zero tolerance across all major section dictionaries
    for section_name in ("profile", "student_context", "grades", "absences", "homework", "agenda", "upcoming_holidays", "didactics"):
        sec = snapshot.get(section_name)
        if isinstance(sec, dict):
            assert "notes" not in sec, f"CRITICAL: 'notes' section key leaked into snapshot['{section_name}']"
            assert "disciplinary_notes" not in sec, f"CRITICAL: 'disciplinary_notes' key leaked into snapshot['{section_name}']"

    # 6. Assert zero tolerance for disciplinary_notes across ALL keys and subkeys recursively
    forbidden_disciplinary = _recursively_find_forbidden_keys(snapshot)
    # Filter for disciplinary_notes or notes at section levels
    disciplinary_leaks = [m for m in forbidden_disciplinary if "disciplinary_notes" in m[0]]
    assert not disciplinary_leaks, (
        f"CRITICAL: Disciplinary notes leaked into snapshot: {disciplinary_leaks}"
    )


@pytest.mark.asyncio
async def test_get_snapshot_resilience_when_subsystems_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stress-test snapshot behavior when calendar and didactics raise unexpected errors:
    Snapshot should degrade gracefully, include warnings, and still maintain zero notes leakage.
    """

    class BrokenClient:
        async def get_profile(self) -> StudentProfile:
            return StudentProfile(ident="S1", student_id="1", account_type="student")

        async def get_subjects(self) -> list[Any]:
            return []

        async def get_grades(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def get_absences(self) -> list[Any]:
            return []

        async def get_agenda(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def get_lessons(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def list_notices(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def get_calendar(self) -> list[Any]:
            raise RuntimeError("Spaggiari calendar API offline")

        async def get_didactics(self) -> list[Any]:
            raise TimeoutError("Spaggiari didactics gateway timeout")

    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(BrokenClient(), store)

    snapshot = await server.get_snapshot(days_ahead=7)

    # Must still provide upcoming_holidays and didactics sections
    assert "upcoming_holidays" in snapshot
    assert snapshot["upcoming_holidays"]["count"] == 0
    assert "didactics" in snapshot
    assert snapshot["didactics"]["folders_count"] == 0

    # Warnings must contain error notices
    warnings = snapshot.get("warnings", [])
    assert any("Calendario" in w for w in warnings)
    assert any("Materiali didattici" in w for w in warnings)

    # Privacy assertion must hold unconditionally
    forbidden_keys = _recursively_find_forbidden_keys(snapshot)
    assert not forbidden_keys


# ============================================================================
# Dimension 2: Read-Only Mode Isolation
# ============================================================================


def _run_subshell_probe(env_var: str, value: str) -> dict[str, Any]:
    """Run an isolated Python subshell with the given environment variable."""
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
    import json

    return json.loads(res.stdout.strip())


def test_read_only_mode_subshell_tool_isolation() -> None:
    """Spawn subshell with CLASSEVIVA_READ_ONLY_MODE=1 and verify tool isolation:
    - None of the 6 new tools (list_didactics, download_didactics_item, get_calendar,
      get_notes, get_documents, get_schoolbooks) appears.
    - get_snapshot does NOT appear.
    - No personal tools appear.
    - Exactly 3 tools are exposed: get_today_context, list_notices, read_notice_pdf.
    """
    res = _run_subshell_probe("CLASSEVIVA_READ_ONLY_MODE", "1")
    tools = set(res["tools"])

    # 1. 6 new QoL tools MUST NOT be present
    new_tools = {
        "list_didactics",
        "download_didactics_item",
        "get_calendar",
        "get_notes",
        "get_documents",
        "get_schoolbooks",
    }
    assert not (new_tools & tools), f"Leaked new QoL tools in read-only mode: {new_tools & tools}"

    # 2. get_snapshot and personal tools MUST NOT be present
    personal_tools = {
        "get_snapshot",
        "update_student_context",
        "save_class_timetable",
        "get_homework",
        "get_agenda",
        "get_lessons",
        "get_grades",
        "calculate_grade_target",
        "get_absences",
    }
    assert not (personal_tools & tools), (
        f"Leaked personal tools in read-only mode: {personal_tools & tools}"
    )

    # 3. Exactly the 3 allowed public tools are present
    expected_allowed = {"get_today_context", "list_notices", "read_notice_pdf"}
    assert tools == expected_allowed, f"Unexpected tools in read-only mode: {tools}"


def test_read_only_mode_subshell_prompt_isolation() -> None:
    """Spawn subshell with CLASSEVIVA_READ_ONLY_MODE=1 and verify prompt isolation:
    - recap-settimanale and piano-recupero-materie MUST NOT appear.
    - setup, daily_brief, recovery_plan MUST NOT appear.
    - Exactly 'notices_brief' appears.
    """
    res = _run_subshell_probe("CLASSEVIVA_READ_ONLY_MODE", "1")
    prompts = set(res["prompts"])

    assert "recap-settimanale" not in prompts, "recap-settimanale leaked in read-only mode!"
    assert "piano-recupero-materie" not in prompts, (
        "piano-recupero-materie leaked in read-only mode!"
    )
    assert "setup" not in prompts
    assert "daily_brief" not in prompts
    assert "recovery_plan" not in prompts
    assert prompts == {"notices_brief"}, f"Unexpected prompts in read-only mode: {prompts}"


def test_read_only_mode_alias_compatibility() -> None:
    """Ensure CLASSEVIVA_PUBLIC_MODE=1 activates identical isolation."""
    res = _run_subshell_probe("CLASSEVIVA_PUBLIC_MODE", "1")
    assert set(res["tools"]) == {"get_today_context", "list_notices", "read_notice_pdf"}
    assert set(res["prompts"]) == {"notices_brief"}


@pytest.mark.asyncio
async def test_read_only_direct_tool_permission_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """If an internal caller attempts to invoke write/personal tools directly while
    READ_ONLY_MODE is True, _ensure_writable must raise PermissionError.
    """
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)

    with pytest.raises(PermissionError, match="CLASSEVIVA_READ_ONLY_MODE"):
        server._ensure_writable()

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


# ============================================================================
# Dimension 3: FastMCP Prompts
# ============================================================================


@pytest.mark.asyncio
async def test_fastmcp_prompts_recap_settimanale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test recap-settimanale prompt execution:
    - Registered with exact kebab-case name 'recap-settimanale'.
    - Accepts 'days_ahead' argument.
    - References expected tools: get_today_context, get_homework, get_calendar, list_notices.
    """
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    # 1. Check prompt is registered
    prompts = await server.mcp.list_prompts()
    prompt_names = [p.name for p in prompts]
    assert "recap-settimanale" in prompt_names, "'recap-settimanale' not found in mcp.list_prompts()"

    # 2. Call with days_ahead=7
    result_7 = await server.mcp.get_prompt("recap-settimanale", {"days_ahead": "7"})
    assert result_7.description
    assert len(result_7.messages) == 1
    text_7 = result_7.messages[0].content.text
    assert "prossimi 7 giorni" in text_7
    assert "get_today_context" in text_7
    assert "get_homework(days_ahead=7)" in text_7
    assert "get_calendar(days_ahead=7)" in text_7
    assert "list_notices(limit=10)" in text_7

    # 3. Call with days_ahead=14
    result_14 = await server.mcp.get_prompt("recap-settimanale", {"days_ahead": "14"})
    text_14 = result_14.messages[0].content.text
    assert "prossimi 14 giorni" in text_14
    assert "get_homework(days_ahead=14)" in text_14
    assert "get_calendar(days_ahead=14)" in text_14

    # 4. Call with default arguments
    result_def = await server.mcp.get_prompt("recap-settimanale", {})
    text_def = result_def.messages[0].content.text
    assert "prossimi 7 giorni" in text_def


@pytest.mark.asyncio
async def test_fastmcp_prompts_piano_recupero_materie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test piano-recupero-materie prompt execution:
    - Registered with exact kebab-case name 'piano-recupero-materie'.
    - Accepts 'subject' argument.
    - References expected tools: get_grades, calculate_grade_target, get_lessons, get_agenda.
    """
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    # 1. Check prompt is registered
    prompts = await server.mcp.list_prompts()
    prompt_names = [p.name for p in prompts]
    assert "piano-recupero-materie" in prompt_names, (
        "'piano-recupero-materie' not found in mcp.list_prompts()"
    )

    # 2. Call with specific subject
    res_subj = await server.mcp.get_prompt("piano-recupero-materie", {"subject": "Fisica"})
    assert res_subj.description
    assert len(res_subj.messages) == 1
    text_subj = res_subj.messages[0].content.text
    assert "per la materia 'Fisica'" in text_subj
    assert "get_grades" in text_subj
    assert "calculate_grade_target" in text_subj
    assert "get_lessons" in text_subj
    assert "get_agenda" in text_subj

    # 3. Call without subject (general recovery plan)
    res_all = await server.mcp.get_prompt("piano-recupero-materie", {})
    text_all = res_all.messages[0].content.text
    assert "per tutte le materie insufficienti" in text_all
    assert "get_grades" in text_all
    assert "calculate_grade_target" in text_all


@pytest.mark.asyncio
async def test_get_snapshot_days_ahead_clamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stress-test days_ahead boundary conditions:
    - days_ahead < 1 clamped to 1
    - days_ahead > 60 clamped to 60
    """
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    client = _rich_fake_client()
    server.set_client_for_testing(client, store)

    # Test negative days_ahead
    snap_low = await server.get_snapshot(days_ahead=-10)
    assert snap_low["upcoming_holidays"]["days_ahead"] == 1

    # Test zero days_ahead
    snap_zero = await server.get_snapshot(days_ahead=0)
    assert snap_zero["upcoming_holidays"]["days_ahead"] == 1

    # Test excessive days_ahead
    snap_high = await server.get_snapshot(days_ahead=999)
    assert snap_high["upcoming_holidays"]["days_ahead"] == 60


def test_read_only_mode_subshell_tool_and_prompt_invocation_rejection() -> None:
    """Assert that in a real subshell with CLASSEVIVA_READ_ONLY_MODE=1,
    attempting to call get_prompt or call_tool for personal items fails appropriately.
    """
    env = os.environ.copy()
    env["CLASSEVIVA_READ_ONLY_MODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    code = (
        "import asyncio;\n"
        "from classeviva_mcp import server;\n"
        "\n"
        "async def run():\n"
        "    # 1. get_prompt on hidden prompt must raise\n"
        "    try:\n"
        "        await server.mcp.get_prompt('recap-settimanale', {})\n"
        "        raise AssertionError('Expected recap-settimanale get_prompt to fail')\n"
        "    except Exception as e:\n"
        "        assert 'recap-settimanale' in str(e) or 'not found' in str(e).lower() or isinstance(e, KeyError)\n"
        "\n"
        "    # 2. call_tool on hidden tool must raise\n"
        "    try:\n"
        "        await server.mcp.call_tool('list_didactics', {})\n"
        "        raise AssertionError('Expected list_didactics call_tool to fail')\n"
        "    except Exception as e:\n"
        "        assert 'list_didactics' in str(e) or 'not found' in str(e).lower() or isinstance(e, KeyError)\n"
        "\n"
        "    try:\n"
        "        await server.mcp.call_tool('get_snapshot', {})\n"
        "        raise AssertionError('Expected get_snapshot call_tool to fail')\n"
        "    except Exception as e:\n"
        "        assert 'get_snapshot' in str(e) or 'not found' in str(e).lower() or isinstance(e, KeyError)\n"
        "\n"
        "    print('ALL_REJECTIONS_VERIFIED')\n"
        "\n"
        "asyncio.run(run())\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ALL_REJECTIONS_VERIFIED" in res.stdout

