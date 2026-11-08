from __future__ import annotations

from pathlib import Path

import pytest

import classeviva_mcp.server as server
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import AbsenceItem


class FakeAbsenceClient:
    async def get_absences(self) -> list[AbsenceItem]:
        return [
            AbsenceItem(id="1", date="2026-10-01", kind="absence", is_justified=False),
            AbsenceItem(id="2", date="2026-10-02", kind="delay", is_justified=True),
            AbsenceItem(id="3", date="2026-10-03", kind="absence", is_justified=True),
        ]


def _fresh_store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)


def test_class_code_normalization_accepts_common_variants() -> None:
    assert server._normalize_class_code("5 ai") == "5AI"
    assert server._normalize_class_code("5ª AI") == "5AI"
    assert server._normalize_class_code(" 5-AI ") == "5AI"
    assert server._normalize_class_code("3ac") == "3AC"


@pytest.mark.parametrize("value", ["", "AI", "5A Informatica", "quinta AI", "5AI数学"])
def test_class_code_normalization_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        server._normalize_class_code(value)


def test_day_and_time_validation() -> None:
    assert server._normalize_day("Lunedì") == "Lunedi"
    assert server._normalize_day("monday") == "Lunedi"
    assert server._validate_hhmm("08:00", "start_time") == "08:00"
    assert server._validate_hhmm("", "start_time") == ""
    with pytest.raises(ValueError):
        server._normalize_day("Domenica")
    with pytest.raises(ValueError):
        server._validate_hhmm("8:00", "start_time")


@pytest.mark.asyncio
async def test_update_student_context_normalizes_class_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    result = await server.update_student_context(
        school_class="5 ai",
        course_name="Informatica",
        register_number=14,
        custom_notes="nota\x00valida " + "x" * 1600,
    )

    assert result["student_context"]["school_class"] == "5AI"
    notes = result["student_context"]["custom_notes"]
    assert "\x00" not in notes
    assert len(notes) <= 1500


@pytest.mark.asyncio
async def test_save_timetable_is_local_only_and_normalizes_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    result = await server.save_class_timetable(
        school_class="5 ai",
        schedule={
            "Lunedì": [
                {
                    "hour": 1,
                    "start_time": "08:00",
                    "end_time": "09:00",
                    "subject": "Informatica",
                    "room": "Lab 2",
                }
            ]
        },
    )

    assert result["status"] == "timetable_saved"
    assert result["storage"] == "local_only"
    assert result["timetable"]["school_class"] == "5AI"
    assert list(result["timetable"]["schedule"]) == ["Lunedi"]
    assert store.load_student_context().school_class == "5AI"


@pytest.mark.asyncio
async def test_save_timetable_rejects_duplicate_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    with pytest.raises(ValueError, match="duplicata"):
        await server.save_class_timetable(
            school_class="5AI",
            schedule={
                "Lunedi": [
                    {"hour": 1, "subject": "Informatica"},
                    {"hour": 1, "subject": "Matematica"},
                ]
            },
        )


@pytest.mark.asyncio
async def test_save_timetable_rejects_bad_time_and_cjk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    with pytest.raises(ValueError, match="HH:MM"):
        await server.save_class_timetable(
            school_class="5AI",
            schedule={"Lunedi": [{"hour": 1, "start_time": "8:00", "subject": "Informatica"}]},
        )

    with pytest.raises(ValueError, match="CJK"):
        await server.save_class_timetable(
            school_class="5AI",
            schedule={"Lunedi": [{"hour": 1, "subject": "Informatica 数学"}]},
        )


@pytest.mark.asyncio
async def test_save_timetable_accepts_a_more_specific_class_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    await server.update_student_context(school_class="5A")

    result = await server.save_class_timetable(
        school_class="5AI",
        schedule={"Lunedi": [{"hour": 1, "subject": "Informatica"}]},
    )

    assert result["timetable"]["school_class"] == "5AI"
    assert store.load_student_context().school_class == "5AI"


@pytest.mark.asyncio
async def test_save_timetable_refuses_a_different_class_without_context_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    await server.update_student_context(school_class="5AI")

    with pytest.raises(ValueError, match="update_student_context"):
        await server.save_class_timetable(
            school_class="5AL",
            schedule={"Lunedi": [{"hour": 1, "subject": "Logistica"}]},
        )


@pytest.mark.asyncio
async def test_absences_summary_never_confuses_total_with_unjustified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _fresh_store(tmp_path)
    server.set_client_for_testing(FakeAbsenceClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    result = await server.get_absences()

    assert result["total_events"] == 3
    assert result["unjustified_count"] == 1
    assert result["justified_count"] == 2
    assert result["breakdown_by_kind"] == {"absence": 2, "delay": 1}
    assert "1 eventi da giustificare su 3 totali" in result["summary"]


@pytest.mark.asyncio
async def test_absences_summary_when_nothing_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AllJustifiedClient:
        async def get_absences(self) -> list[AbsenceItem]:
            return [
                AbsenceItem(id=str(i), date="2026-10-01", kind="absence", is_justified=True)
                for i in range(3)
            ]

    store = _fresh_store(tmp_path)
    server.set_client_for_testing(AllJustifiedClient(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)

    result = await server.get_absences()

    assert result["total_events"] == 3
    assert result["unjustified_count"] == 0
    assert result["summary"] == "Nessun evento da giustificare su 3 eventi totali."


def test_unknown_agenda_kind_falls_back_with_warning() -> None:
    kind, warning = server._normalize_kind("verifica")
    assert kind == "exam"
    assert warning is None

    kind, warning = server._normalize_kind("boh")
    assert kind == "all"
    assert warning is not None and "non riconosciuto" in warning
