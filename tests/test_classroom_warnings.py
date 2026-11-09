from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import classeviva_mcp.classroom_ical as classroom
import classeviva_mcp.server as server
from classeviva_mcp.classroom_ical import ClassroomFetchResult, _ical_warning
from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.models import AgendaItem, StudentProfile

SECRET_URL = "https://calendar.google.com/calendar/ical/SECRET123/basic.ics"


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", SECRET_URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def test_ical_warning_404_tells_the_user_to_regenerate_without_leaking_url() -> None:
    warning = _ical_warning(_http_error(404))

    assert "HTTP 404" in warning
    assert "set-classroom-url" in warning
    assert "SECRET123" not in warning


def test_ical_warning_410_is_treated_as_expired() -> None:
    warning = _ical_warning(_http_error(410))

    assert "HTTP 410" in warning
    assert "non più valido" in warning


def test_ical_warning_generic_errors_have_no_url_or_token() -> None:
    warning = _ical_warning(ValueError("bad feed " + SECRET_URL))

    assert "ValueError" in warning
    assert "SECRET123" not in warning


@pytest.mark.asyncio
async def test_fetch_classroom_tasks_returns_warnings_instead_of_swallowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[AgendaItem]:
        raise _http_error(404)

    monkeypatch.setattr(classroom, "fetch_classroom_ical_tasks", boom)

    result = await classroom.fetch_classroom_tasks(ical_url=SECRET_URL)

    assert isinstance(result, ClassroomFetchResult)
    assert result.items == []
    assert len(result.warnings) == 1
    assert "HTTP 404" in result.warnings[0]


@pytest.mark.asyncio
async def test_fetch_classroom_tasks_keeps_partial_items_when_one_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[AgendaItem]:
        raise _http_error(404)

    async def ok(*args: Any, **kwargs: Any) -> list[AgendaItem]:
        return [
            AgendaItem(
                id="gc:1",
                source="classroom_oauth",
                kind="assignment",
                subject="Informatica",
                title="Esercizio",
                date="2026-11-20",
            )
        ]

    monkeypatch.setattr(classroom, "fetch_classroom_ical_tasks", boom)
    monkeypatch.setattr(classroom, "fetch_classroom_oauth_tasks", ok)

    result = await classroom.fetch_classroom_tasks(
        ical_url=SECRET_URL, google_access_token="oauth-token"
    )

    assert len(result.items) == 1
    assert result.items[0].id == "gc:1"
    assert len(result.warnings) == 1
    assert "oauth-token" not in result.warnings[0]


class _EmptyAgendaClient:
    async def get_profile(self) -> StudentProfile:
        return StudentProfile(
            ident="S1234567X",
            student_id="1234567",
            account_type="student",
            school_class="5AI",
        )

    async def get_subjects(self) -> list[Any]:
        return []

    async def get_grades(self, subject_filter: str | None = None) -> list[Any]:
        return []

    async def get_absences(self) -> list[Any]:
        return []

    async def get_agenda(self, start: str, end: str) -> list[AgendaItem]:
        return []


class _StoreWithClassroom:
    def load(self) -> StoredCredentials:
        return StoredCredentials(
            username="S1234567X",
            password="unused",
            classroom_ical_url=SECRET_URL,
        )


@pytest.mark.asyncio
async def test_get_homework_surfaces_classroom_warnings_in_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.set_client_for_testing(_EmptyAgendaClient(), _StoreWithClassroom())  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    async def fake_fetch(**kwargs: Any) -> ClassroomFetchResult:
        return ClassroomFetchResult(warnings=["Google Classroom (iCal): HTTP 404."])

    monkeypatch.setattr(server, "fetch_classroom_tasks", fake_fetch)

    result = await server.get_homework(days_ahead=7)

    assert result["warnings"] == ["Google Classroom (iCal): HTTP 404."]
    assert "Attenzione" in result["summary"]
    assert "HTTP 404" in result["summary"]
    assert result["breakdown"] == {"homework": 0, "exam": 0, "assignment": 0, "note": 0}


@pytest.mark.asyncio
async def test_get_snapshot_aggregates_homework_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    # Classroom must be configured, otherwise the fetch is (correctly) skipped.
    monkeypatch.setattr(
        store,
        "load",
        lambda: StoredCredentials(
            username="S1234567X",
            password="pw",
            classroom_ical_url="https://calendar.google.com/calendar/ical/x/private-y/basic.ics",
        ),
    )
    server.set_client_for_testing(_EmptyAgendaClient(), store)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    async def fake_fetch(**kwargs: Any) -> ClassroomFetchResult:
        return ClassroomFetchResult(warnings=["Classroom non disponibile."])

    monkeypatch.setattr(server, "fetch_classroom_tasks", fake_fetch)

    snapshot = await server.get_snapshot(days_ahead=7)

    assert "Classroom non disponibile." in snapshot["warnings"]
    assert "Classroom non disponibile." in snapshot["homework"]["warnings"]
    # Each section keeps its own warnings; the Classroom one is not duplicated into grades.
    assert "Classroom non disponibile." not in snapshot["grades"]["warnings"]
    assert snapshot["grades"]["warnings"] == ["Nessun voto disponibile sul registro."]
