"""get_today_context stays compact: today's and next school day's lessons by default,
the full weekly timetable only on request, never internal bookkeeping."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import classeviva_mcp.server as server
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import StudentProfile


class _Client:
    async def get_profile(self) -> StudentProfile:
        return StudentProfile(ident="S1X", student_id="1", account_type="student", school_class="5AI")

    async def get_subjects(self) -> list[Any]:
        return []


def _freeze(monkeypatch: pytest.MonkeyPatch, year: int, month: int, day: int) -> None:
    moment = datetime(year, month, day, 7, 30, tzinfo=ZoneInfo("Europe/Rome"))
    monkeypatch.setattr(server, "now_rome", lambda: moment)


async def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CredentialStore:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(store, "has_classroom_ical_url", lambda: False)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(_Client(), store)  # type: ignore[arg-type]
    await server.save_class_timetable(
        school_class="5AI",
        # Deliberately out of week order: the output must be ordered.
        schedule={
            "Venerdi": [{"hour": 1, "start_time": "08:00", "subject": "Matematica"}],
            "Lunedi": [
                {"hour": 2, "start_time": "09:00", "subject": "Inglese"},
                {"hour": 1, "start_time": "08:00", "subject": "Italiano", "room": "318"},
            ],
        },
        valid_from="2026-11-01",
    )
    store.mark_notices_seen(["111", "222"])
    return store


@pytest.mark.asyncio
async def test_default_is_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(tmp_path, monkeypatch)
    _freeze(monkeypatch, 2026, 11, 16)  # Monday

    ctx = await server.get_today_context()

    assert ctx["weekday"] == "Lunedì"
    student = ctx["student_context"]
    assert "timetable" not in student
    assert "seen_notice_ids" not in student
    assert student["timetable_summary"]["saved"] is True
    assert student["timetable_summary"]["days"] == ["Lunedi", "Venerdi"]

    today = ctx["lessons_today"]
    assert today["date"] == "2026-11-16"
    assert [slot["subject"] for slot in today["lessons"]] == ["Italiano", "Inglese"]
    # Empty optional fields are dropped from the compact lessons list.
    assert "teacher" not in today["lessons"][0]
    assert today["lessons"][0]["room"] == "318"

    nxt = ctx["lessons_next_school_day"]
    assert nxt["date"] == "2026-11-20"
    assert nxt["weekday"] == "Venerdì"


@pytest.mark.asyncio
async def test_weekend_has_no_lessons_and_next_day_wraps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(tmp_path, monkeypatch)
    _freeze(monkeypatch, 2026, 11, 22)  # Sunday

    ctx = await server.get_today_context()

    assert ctx["weekday"] == "Domenica"
    assert ctx["lessons_today"]["lessons"] == []
    assert ctx["lessons_next_school_day"]["date"] == "2026-11-23"


@pytest.mark.asyncio
async def test_full_timetable_on_request_in_week_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(tmp_path, monkeypatch)
    _freeze(monkeypatch, 2026, 11, 16)

    ctx = await server.get_today_context(include_timetable=True)

    timetable = ctx["student_context"]["timetable"]
    assert list(timetable["schedule"]) == ["Lunedi", "Venerdi"]
    assert "timetable_summary" not in ctx["student_context"]
    assert "seen_notice_ids" not in ctx["student_context"]


@pytest.mark.asyncio
async def test_without_timetable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    monkeypatch.setattr(store, "has_classroom_ical_url", lambda: False)
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    server.set_client_for_testing(_Client(), store)  # type: ignore[arg-type]

    ctx = await server.get_today_context()

    assert ctx["student_context"]["timetable_summary"] == {"saved": False}
    assert ctx["lessons_today"] is None
    assert ctx["lessons_next_school_day"] is None
