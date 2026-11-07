from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import classeviva_mcp.server as server
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import StudentProfile


class FakeClient:
    async def get_profile(self) -> StudentProfile:
        return StudentProfile(
            ident="S1234567X",
            student_id="1234567",
            account_type="student",
            first_name="Luca",
            last_name="Verdi",
            school_class="5AI",
            school_name="ITIS Galilei",
        )

    async def get_subjects(self) -> list[Any]:
        return []

    async def get_grades(self, subject_filter: str | None = None) -> list[Any]:
        return []

    async def get_absences(self) -> list[Any]:
        return []

    async def get_agenda(self, start: str, end: str) -> list[Any]:
        return []

    async def get_lessons(self, start: str, end: str, subject_filter: str | None = None) -> list[Any]:
        return []

    async def list_notices(self, query: str | None = None, unread_only: bool = False) -> list[Any]:
        return []

    async def read_notice_content(
        self,
        event_code: str,
        pub_id: str,
        attach_num: int | None = None,
        *,
        attachment_index: int | None = None,
        allow_mark_as_read: bool = False,
    ) -> dict[str, Any]:
        return {
            "text_body": "Titolo",
            "notice_title": "Titolo",
            "pdf_bytes": b"",
            "attachments": [],
            "marked_as_read": False,
            "was_unread": True,
            "skipped": True,
        }


@pytest.mark.asyncio
async def test_personal_mode_hides_ical_url_and_keeps_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(store, "has_classroom_ical_url", lambda: False)
    server.set_client_for_testing(FakeClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    result = await server.get_today_context()
    assert result["read_only_mode"] is False
    assert "classroom_ical_url" not in result["student_context"]
    assert result["student_context"]["classroom_ical_configured"] is False
    assert result["profile"]["ident"] == "S1234567X"
    assert result["profile"]["first_name"] == "Luca"


@pytest.mark.asyncio
async def test_read_only_mode_minimizes_pii_and_blocks_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(store, "has_classroom_ical_url", lambda: False)
    server.set_client_for_testing(FakeClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)

    result = await server.get_today_context()
    assert result["read_only_mode"] is True
    assert "ident" not in result["profile"]
    assert "student_id" not in result["profile"]
    assert "first_name" not in result["profile"]
    assert "last_name" not in result["profile"]
    assert "register_number" not in result["student_context"]
    assert "custom_notes" not in result["student_context"]
    assert "classroom_ical_url" not in result["student_context"]

    with pytest.raises(PermissionError):
        await server.update_student_context(school_class="5AI")
    with pytest.raises(PermissionError):
        await server.save_class_timetable(school_class="5AI", schedule={})
    with pytest.raises(PermissionError):
        await server.get_grades()
    with pytest.raises(PermissionError):
        await server.calculate_grade_target("Informatica")
    with pytest.raises(PermissionError):
        await server.get_absences()
    with pytest.raises(PermissionError):
        await server.get_homework()
    with pytest.raises(PermissionError):
        await server.get_agenda()
    with pytest.raises(PermissionError):
        await server.get_lessons()


@pytest.mark.asyncio
async def test_read_only_notice_reading_never_marks_or_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(store, "has_classroom_ical_url", lambda: False)
    server.set_client_for_testing(FakeClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", True)

    result = await server.read_notice_pdf(notice_id="1", save_pdf=True, allow_mark_as_read=True)
    assert result["notice_marked_as_read"] is False
    assert result["notice_was_unread"] is True
    assert result["saved_files"] == []
    assert result["rendered_page_images"] == []


def test_read_only_tool_registry_via_subprocess() -> None:
    env = os.environ.copy()
    env["CLASSEVIVA_READ_ONLY_MODE"] = "1"
    env.pop("CLASSEVIVA_PUBLIC_MODE", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import asyncio;"
        "from classeviva_mcp.server import mcp;"
        "print(sorted(t.name for t in asyncio.run(mcp.list_tools())));"
        "print(sorted(p.name for p in asyncio.run(mcp.list_prompts())))"
    )
    output = subprocess.check_output(
        [sys.executable, "-c", code], env=env, text=True, stderr=subprocess.DEVNULL
    )
    assert "get_today_context" in output
    assert "list_notices" in output
    assert "read_notice_pdf" in output
    for forbidden in (
        "get_grades",
        "calculate_grade_target",
        "get_absences",
        "get_homework",
        "get_agenda",
        "get_lessons",
        "update_student_context",
        "save_class_timetable",
        "recovery_plan",
        "daily_brief",
    ):
        assert forbidden not in output


def test_public_mode_alias_enables_read_only() -> None:
    env = os.environ.copy()
    env.pop("CLASSEVIVA_READ_ONLY_MODE", None)
    env["CLASSEVIVA_PUBLIC_MODE"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "from classeviva_mcp.server import READ_ONLY_MODE;"
        "print(READ_ONLY_MODE)"
    )
    output = subprocess.check_output(
        [sys.executable, "-c", code], env=env, text=True, stderr=subprocess.DEVNULL
    )
    assert output.strip() == "True"
