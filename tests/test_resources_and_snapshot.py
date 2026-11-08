from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import classeviva_mcp.server as server
from classeviva_mcp.credentials import CredentialStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_client() -> Any:
    """A stub client returning deterministic data for every read-only endpoint."""

    class Fake:
        async def get_profile(self) -> Any:
            from classeviva_mcp.models import StudentProfile

            return StudentProfile(
                ident="S1234567X",
                student_id="1234567",
                account_type="student",
                first_name="Luca",
                school_class="5AI",
                school_name="ITIS Galilei",
            )

        async def get_subjects(self) -> list[Any]:
            from classeviva_mcp.models import SubjectTeacher

            return [SubjectTeacher(subject_id="s1", subject_name="Informatica", teachers=["Rossi"])]

        async def get_grades(self, subject_filter: str | None = None) -> list[Any]:
            from classeviva_mcp.models import GradeItem

            return [
                GradeItem(
                    id="1",
                    subject="Informatica",
                    value=8.0,
                    display_value="8",
                    weight=1.0,
                    date="2026-10-25",
                    period="Primo trimestre",
                    category="Scritto",
                )
            ]

        async def get_absences(self) -> list[Any]:
            from classeviva_mcp.models import AbsenceItem

            return [
                AbsenceItem(
                    id="9",
                    date="2026-10-14",
                    kind="absence",
                    is_justified=False,
                    reason="",
                )
            ]

        async def get_agenda(self, start_date: str, end_date: str) -> list[Any]:
            from classeviva_mcp.models import AgendaItem

            return [
                AgendaItem(
                    id="h1",
                    source="classeviva",
                    kind="homework",
                    subject="Informatica",
                    title="Compito di prova",
                    date="2026-11-10",
                )
            ]

        async def get_lessons(self, start_date: str, end_date: str, subject_filter: str | None = None) -> list[Any]:
            return []

        async def list_notices(self, query: str | None = None, unread_only: bool = False) -> list[Any]:
            from classeviva_mcp.models import NoticeItem

            return [
                NoticeItem(
                    id="245",
                    event_code="CF",
                    title="Circolare",
                    publish_date="2026-11-06",
                    category="Didattica",
                    is_read=False,
                    has_attachment=True,
                )
            ]

    return Fake()


@pytest.mark.asyncio
async def test_get_snapshot_returns_everything_in_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(_fake_client(), store)

    snapshot = await server.get_snapshot(days_ahead=7)

    assert snapshot["profile"]["school_class"] == "5AI"
    assert snapshot["grades"]["overall_average"] == 8.0
    assert snapshot["grades"]["grades_count"] == 1
    assert snapshot["absences"]["unjustified_count"] == 1
    assert snapshot["homework"]["count"] == 1
    assert snapshot["agenda"]["count"] == 1
    assert snapshot["subjects_and_teachers"][0]["subject_name"] == "Informatica"


@pytest.mark.asyncio
async def test_snapshot_respects_the_days_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(_fake_client(), store)

    snapshot = await server.get_snapshot(days_ahead=3)

    assert snapshot["range"]["start_date"] <= snapshot["range"]["end_date"]


def _registry(read_only: bool) -> tuple[list[str], list[str], list[str]]:
    env = os.environ.copy()
    env["CLASSEVIVA_READ_ONLY_MODE"] = "1" if read_only else "0"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    code = (
        "import asyncio;"
        "from classeviva_mcp.server import mcp;"
        "print(','.join(sorted(t.name for t in asyncio.run(mcp.list_tools()))));"
        "print(','.join(sorted(r.name for r in asyncio.run(mcp.list_resources()))));"
        "print(','.join(sorted(t.name for t in asyncio.run(mcp.list_resource_templates()))))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True).splitlines()
    return (
        [x for x in out[0].split(",") if x],
        [x for x in out[1].split(",") if x],
        [x for x in out[2].split(",") if x],
    )


def test_personal_mode_registers_the_snapshot_tool_and_the_resources() -> None:
    tools, resources, templates = _registry(read_only=False)

    assert "get_snapshot" in tools
    assert {"profile", "subjects", "notices", "grades", "absences"} <= set(resources)
    assert {"homework", "agenda", "lessons"} <= set(templates)


def test_read_only_mode_hides_the_personal_tools_and_resources() -> None:
    tools, resources, templates = _registry(read_only=True)

    assert "get_snapshot" not in tools
    assert "get_grades" not in tools
    assert "grades" not in resources
    assert "absences" not in resources
    assert templates == []
    # The non-personal resources stay available.
    assert {"profile", "subjects", "notices"} <= set(resources)
