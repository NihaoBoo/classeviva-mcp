from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import classeviva_mcp.classroom_ical as classroom
import classeviva_mcp.server as server
from classeviva_mcp.classroom_ical import ClassroomFetchResult, _ical_warning
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import AgendaItem, StudentProfile

SECRET_URL = "https://calendar.google.com/calendar/ical/SECRET123/basic.ics"


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", SECRET_URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.asyncio
async def test_classroom_paginates_both_levels_and_preserves_partial_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = request.url.params.get("pageToken")
        if path == "/v1/courses":
            return httpx.Response(200, json=(
                {"courses": [{"id": "1", "name": "Reti"}], "nextPageToken": "courses-2"}
                if not page else {"courses": [{"id": "2"}, {"id": "3", "name": "Storia"}]}
            ))
        if "/2/" in path:
            return httpx.Response(503, json={"error": "synthetic-private-message"})
        if "/1/" in path and not page:
            return httpx.Response(200, json={"nextPageToken": "work-2"})
        return httpx.Response(200, json={"courseWork": [{
            "id": "10",
            "title": "Esercizio",
            "dueDate": {"year": 2026, "month": 11, "day": 24},
            "dueTime": {"hours": 23},
        }]})

    monkeypatch.setattr(classroom, "build_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(respond), **kw
    ))
    result = await classroom.fetch_classroom_tasks(
        google_access_token="synthetic-token", start_date="2026-11-25", end_date="2026-11-25"
    )
    assert {item.subject for item in result.items} == {"Reti", "Storia"}
    assert len({item.id for item in result.items}) == 2
    assert {item.time for item in result.items} == {"00:00"}
    assert len(result.warnings) == 1 and "incompleta" in result.warnings[0]
    assert "synthetic" not in result.warnings[0]


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

    async def ok(*args: Any, **kwargs: Any) -> ClassroomFetchResult:
        return ClassroomFetchResult(items=[
            AgendaItem(
                id="gc:1",
                source="classroom_oauth",
                kind="assignment",
                subject="Informatica",
                title="Esercizio",
                date="2026-11-20",
            )
        ], warnings=["Google Classroom: verifica incompleta."])

    monkeypatch.setattr(classroom, "fetch_classroom_ical_tasks", boom)
    monkeypatch.setattr(classroom, "fetch_classroom_oauth_tasks", ok)

    result = await classroom.fetch_classroom_tasks(
        ical_url=SECRET_URL, google_access_token="oauth-token"
    )

    assert len(result.items) == 1
    assert result.items[0].id == "gc:1"
    assert len(result.warnings) == 2
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


@pytest.mark.asyncio
async def test_get_homework_surfaces_classroom_warnings_without_repeating_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialStore(fallback_dir=tmp_path)
    monkeypatch.setattr(store, "load_classroom_sources", lambda: (SECRET_URL, ""))
    monkeypatch.setattr(store, "load_classroom_grant", lambda: {})
    monkeypatch.setattr(server, "_client", _EmptyAgendaClient())
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    async def fake_fetch(**kwargs: Any) -> ClassroomFetchResult:
        return ClassroomFetchResult(warnings=["Google Classroom (iCal): HTTP 404."])

    monkeypatch.setattr(server, "fetch_classroom_tasks", fake_fetch)

    result = await server.get_homework(days_ahead=7)

    assert result["warnings"] == ["Google Classroom (iCal): HTTP 404."]
    assert "summary" not in result
    assert result["breakdown"] == {"homework": 0, "exam": 0, "assignment": 0, "note": 0}


@pytest.mark.asyncio
async def test_get_snapshot_aggregates_homework_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    # Classroom must be configured, otherwise the fetch is (correctly) skipped.
    monkeypatch.setattr(
        store,
        "load_classroom_sources",
        lambda: (SECRET_URL, ""),
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
