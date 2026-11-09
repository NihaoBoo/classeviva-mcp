from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import unicodedata
from datetime import date, timedelta
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[attr-defined,no-redef]

from classeviva_mcp.classroom_ical import fetch_classroom_tasks
from classeviva_mcp.client import SpaggiariClient
from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.models import (
    ClassTimetable,
    ProfileOut,
    PublicProfileOut,
    PublicStudentContextOut,
    StudentContextOut,
    TimetableSlot,
    current_school_year_str,
    now_rome,
    today_rome_str,
)
from classeviva_mcp.pdf_utils import (
    extract_attachment_text,
    extract_pdf_text,
    render_scanned_pdf_pages,
    save_attachment_file,
)

# Ensure all logs go to stderr so stdio JSON-RPC is never corrupted
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# CLASSEVIVA_PUBLIC_MODE is kept as a backward-compatible alias.
READ_ONLY_MODE = _env_flag("CLASSEVIVA_READ_ONLY_MODE") or _env_flag("CLASSEVIVA_PUBLIC_MODE")

if READ_ONLY_MODE:
    AGENT_INSTRUCTIONS = (
        "Best practice per l'uso di classeviva-mcp in modalità READ-ONLY:\n"
        "1. Chiama `get_today_context()` per leggere data/ora, anno scolastico, materie/professori e orario. "
        "In questa modalità il profilo è minimizzato e non contiene dati anagrafici.\n"
        "2. Usa `list_notices()` per elencare le circolari e `read_notice_pdf()` per leggere un allegato. "
        "Su una circolare non ancora letta, il server non la contrassegna come letta e restituisce solo i metadati: "
        "chiedi conferma all'utente prima di usare `allow_mark_as_read=True`.\n"
        "3. Non tentare di modificare il contesto studente: i tool di scrittura e i dati personali sono disabilitati."
    )
else:
    AGENT_INSTRUCTIONS = (
        "Best practice per l'uso di classeviva-mcp:\n"
        "1. Chiama sempre `get_today_context()` all'inizio della sessione per leggere data/ora corrente in Italia, "
        "l'elenco reale delle materie/professori e il contesto studente (`student_context`: sigla classe es. '5AI', "
        "indirizzo, convenzioni delle sigle dell'istituto in `custom_notes`, e orario salvato).\n"
        "2. Sei TU (l'AI) a interpretare il significato delle sigle della scuola dello studente (es. '5AI' = 5A Informatica, "
        "'5AC' = Chimica, '5AM' = Meccanica, '5AL' = Logistica). Quando apri una circolare o un tabellone orario lungo "
        "con `read_notice_pdf`, passa in `filter_keyword` i termini separati da virgola adatti a quella scuola "
        "(es. `filter_keyword='5AI, Informatica'`). Se la circolare è nuova, chiedi conferma allo studente "
        "prima di usare `allow_mark_as_read=True`, perché l'apertura la segna come letta sul registro.\n"
        "3. Se l'utente ti spiega la sua classe, il significato delle sigle del suo istituto, il numero sul registro o un "
        "nuovo orario scolastico, salvali subito con `update_student_context` o `save_class_timetable`. "
        "L'orario e le note personali restano **solo in locale** sul dispositivo: non vengono mai inviati al registro "
        "scolastico. Non aggiungere mai lingue o caratteri non richiesti (es. cinese/giapponese).\n"
        "4. Il link segreto .ics di Google Classroom NON va incollato nella chat: l'utente deve eseguire "
        "`classeviva-mcp set-classroom-url` nel terminale, così il segreto non passa dal provider AI.\n"
        "5. Non inventare numeri: per le assenze da giustificare usa `unjustified_count` (0 = nessuna), "
        "non `total_events` che conta anche ritardi, uscite e assenze già giustificate. "
        "Se un tool restituisce `warnings`, riportali allo studente invece di ignorarli."
    )

mcp = MCPServer("classeviva-mcp", instructions=AGENT_INSTRUCTIONS)

_store = CredentialStore()
_client = SpaggiariClient(store=_store)


def set_client_for_testing(client: SpaggiariClient, store: CredentialStore | None = None) -> None:
    global _client, _store
    _client = client
    if store is not None:
        _store = store


def _ensure_writable() -> None:
    if READ_ONLY_MODE:
        raise PermissionError(
            "Operazione non consentita: il server è configurato in CLASSEVIVA_READ_ONLY_MODE."
        )


_AGENDA_KIND_ALIASES = {
    "all": "all",
    "tutto": "all",
    "tutti": "all",
    "homework": "homework",
    "compito": "homework",
    "compiti": "homework",
    "consegna": "homework",
    "consegne": "homework",
    "exam": "exam",
    "esame": "exam",
    "esami": "exam",
    "verifica": "exam",
    "verifiche": "exam",
    "interrogazione": "exam",
    "interrogazioni": "exam",
    "note": "note",
    "nota": "note",
    "annotazione": "note",
    "annotazioni": "note",
    "assignment": "assignment",
}


def _normalize_kind(kind: str) -> tuple[str, str | None]:
    """Maps Italian/English synonyms to a valid kind, returning a warning if unknown."""
    clean = (kind or "all").strip().lower()
    mapped = _AGENDA_KIND_ALIASES.get(clean)
    if mapped is None:
        allowed = "all, homework, exam, note"
        return "all", (
            f"Valore `kind='{kind}'` non riconosciuto: uso 'all'. Valori ammessi: {allowed} "
            "(in italiano: verifiche/interrogazioni -> exam, compiti -> homework)."
        )
    return mapped, None


def _validated_date(value: str, fallback: str, field_name: str) -> tuple[str, str | None]:
    """Validates a YYYY-MM-DD argument; falls back with a warning instead of a raw HTTP error."""
    clean = (value or "").strip()
    if not clean:
        return fallback, None
    try:
        return date.fromisoformat(clean).isoformat(), None
    except ValueError:
        return fallback, (
            f"`{field_name}='{clean}'` non è una data valida: uso {fallback}. "
            "Il formato richiesto è YYYY-MM-DD."
        )


def _clean_local_text(value: str, max_len: int) -> str:
    """Strips control characters and bounds a locally stored free-text field."""
    cleaned = "".join(ch for ch in (value or "") if ch.isprintable() or ch == " ")
    return " ".join(cleaned.split())[:max_len]


def _looks_like_cjk(value: str) -> bool:
    return any(
        "\u3040" <= ch <= "\u30ff"  # Hiragana/Katakana
        or "\u3400" <= ch <= "\u4dbf"  # CJK Extension A
        or "\u4e00" <= ch <= "\u9fff"  # CJK Unified Ideographs
        for ch in value
    )


_DAY_ALIASES = {
    "lunedi": "Lunedi",
    "lun": "Lunedi",
    "monday": "Lunedi",
    "martedi": "Martedi",
    "mar": "Martedi",
    "tuesday": "Martedi",
    "mercoledi": "Mercoledi",
    "mer": "Mercoledi",
    "wednesday": "Mercoledi",
    "giovedi": "Giovedi",
    "gio": "Giovedi",
    "thursday": "Giovedi",
    "venerdi": "Venerdi",
    "ven": "Venerdi",
    "friday": "Venerdi",
    "sabato": "Sabato",
    "sab": "Sabato",
    "saturday": "Sabato",
}
_CLASS_CODE_RE = re.compile(r"^\d{1,2}[A-Z]{1,3}$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def _normalize_class_code(value: str) -> str:
    """Normalizes '5 ai', '5ª AI', '5-AI' to the canonical '5AI'."""
    if _looks_like_cjk(value):
        raise ValueError(
            f"Classe '{value}' non valida: contiene caratteri CJK non ammessi."
        )
    cleaned = _strip_accents(_clean_local_text(value, 20).replace("ª", " ").replace("º", " ")).upper()
    compact = "".join(ch for ch in cleaned if ch.isascii() and ch.isalnum())
    if not compact:
        raise ValueError("La classe non può essere vuota: usa la sigla, es. '5AI'.")
    if not _CLASS_CODE_RE.fullmatch(compact):
        raise ValueError(
            f"Classe '{value}' non valida: usa la sigla compatta senza spazi, "
            "es. '5AI' (classe 5, sezione AI)."
        )
    return compact


def _classes_compatible(saved: str, incoming: str) -> bool:
    """Treats '5A' and '5AI' as the same class section, but not '5AI' and '5AL'."""
    return saved == incoming or saved.startswith(incoming) or incoming.startswith(saved)


def _normalize_day(value: str) -> str:
    key = _strip_accents(_clean_local_text(value, 20)).lower().replace(" ", "")
    day = _DAY_ALIASES.get(key)
    if day is None:
        raise ValueError(
            f"Giorno '{value}' non valido: usa Lunedi, Martedi, Mercoledi, "
            "Giovedi, Venerdi o Sabato."
        )
    return day


def _validate_hhmm(value: str, field_name: str) -> str:
    clean = (value or "").strip()
    if not clean:
        return ""
    if not _TIME_RE.fullmatch(clean):
        raise ValueError(
            f"`{field_name}` = '{value}' non è un orario valido: usa HH:MM (es. '08:00')."
        )
    return clean


def _reject_cjk(value: str, field_name: str) -> None:
    """Blocks hallucinated Chinese/Japanese characters in locally saved fields."""
    if _looks_like_cjk(value):
        raise ValueError(
            f"`{field_name}` contiene caratteri CJK (cinesi/giapponesi) non ammessi: "
            "probabile errore di generazione. Riscrivi il valore in italiano."
        )


def _profile_out(profile: Any) -> dict[str, Any]:
    if READ_ONLY_MODE:
        return PublicProfileOut(
            school_class=profile.school_class,
            school_name=profile.school_name,
        ).model_dump()
    return ProfileOut(
        ident=profile.ident,
        student_id=profile.student_id,
        account_type=profile.account_type,
        first_name=profile.first_name,
        last_name=profile.last_name,
        school_class=profile.school_class,
        school_name=profile.school_name,
    ).model_dump()


def _context_out(ctx: Any) -> dict[str, Any]:
    configured = _store.has_classroom_ical_url()
    if READ_ONLY_MODE:
        return PublicStudentContextOut(
            school_class=ctx.school_class,
            course_name=ctx.course_name,
            school_year=ctx.school_year,
            classroom_ical_configured=configured,
            timetable=ctx.timetable,
        ).model_dump()
    return StudentContextOut(
        school_class=ctx.school_class,
        course_name=ctx.course_name,
        register_number=ctx.register_number,
        custom_notes=ctx.custom_notes,
        school_year=ctx.school_year,
        classroom_ical_configured=configured,
        timetable=ctx.timetable,
        seen_notice_ids=ctx.seen_notice_ids,
        updated_at=ctx.updated_at,
    ).model_dump()


@mcp.tool()
async def get_today_context() -> dict[str, Any]:
    """Restituisce la data/ora corrente in Italia (Europe/Rome), l'anno scolastico corrente, il profilo scuola,
    l'elenco materie/professori e il contesto persistente salvato in locale (classe es. '5AI', indirizzo, orario).
    Verifica inoltre in automatico se è cambiato l'anno scolastico o la classe sul registro elettronico (es. passaggio da 3AI a 4AI).
    In modalità read-only il profilo è minimizzato e non contiene dati anagrafici né segreti.
    """
    now = now_rome()
    current_sy = current_school_year_str(now)
    profile = await _client.get_profile()
    student_ctx = _store.load_student_context()

    register_class = (profile.school_class or "").strip().upper()
    saved_class = (student_ctx.school_class or "").strip().upper()
    saved_sy = student_ctx.school_year or current_sy

    # Detect school-year rollover or register promotion (e.g. 3AI -> 4AI)
    year_changed = bool(student_ctx.school_year and student_ctx.school_year != current_sy)
    class_mismatch = bool(
        register_class
        and saved_class
        and register_class[:1].isdigit()
        and saved_class[:1].isdigit()
        and register_class[:1] != saved_class[:1]
    )
    needs_review = year_changed or class_mismatch

    if not profile.school_class and saved_class:
        profile.school_class = saved_class

    subjects_list: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        subjects = await _client.get_subjects()
        subjects_list = [s.model_dump() for s in subjects]
    except Exception as exc:  # noqa: BLE001 - keep the rest of the context usable
        logger.warning("Elenco materie non disponibile: %s", type(exc).__name__)
        warnings.append(
            "Elenco materie/professori non disponibile in questo momento (errore API): "
            "`subjects_and_teachers` è vuoto, non significa che non ci siano materie."
        )

    review_hint = None
    if needs_review:
        if READ_ONLY_MODE:
            review_hint = (
                f"Rilevato possibile cambio di anno scolastico ({saved_sy} -> {current_sy}) "
                f"o di classe sul registro (salvata='{saved_class}', registro='{register_class}'). "
                "In modalità read-only non è possibile aggiornare il contesto: chiedi allo studente di verificare."
            )
        else:
            review_hint = (
                f"Rilevato possibile cambio di anno scolastico ({saved_sy} -> {current_sy}) "
                f"o di classe sul registro ( salvata='{saved_class}', registro='{register_class}' ). "
                "Chiedi allo studente se è passato alla nuova classe e aggiorna con `update_student_context`."
            )

    return {
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "current_time_rome": now.strftime("%H:%M"),
        "school_year": current_sy,
        "school_year_status": {
            "current_school_year": current_sy,
            "saved_school_year": saved_sy,
            # Internal code returned by the register (e.g. ALTF080003): it is NOT
            # the class label shown to the student (e.g. 5AI).
            "register_class_code": register_class or None,
            "needs_class_review": needs_review,
            "agent_hint": review_hint,
        },
        "read_only_mode": READ_ONLY_MODE,
        "public_mode_active": READ_ONLY_MODE,  # backward-compatible field
        "profile": _profile_out(profile),
        "student_context": _context_out(student_ctx),
        "subjects_and_teachers": subjects_list,
        "warnings": warnings,
    }


async def update_student_context(
    school_class: str = "",
    course_name: str = "",
    register_number: int | None = None,
    custom_notes: str = "",
) -> dict[str, Any]:
    """Salva in modo persistente sul dispositivo le informazioni dello studente che non sempre compaiono su Spaggiari:
    - `school_class`: sigla classe e sezione (es. '5AI')
    - `course_name`: indirizzo di studio (es. 'Informatica')
    - `register_number`: numero sull'elenco del registro di classe (utile per interrogazioni/sorteggi)
    - `custom_notes`: preferenze o note aggiuntive (max 1500 caratteri)
    I campi lasciati vuoti mantengono il valore precedente. Il link iCal segreto non si inserisce da qui:
    usa il comando `classeviva-mcp set-classroom-url` nel terminale.
    """
    _ensure_writable()
    ctx = _store.load_student_context()
    if school_class.strip():
        ctx.school_class = _normalize_class_code(school_class)
    if course_name.strip():
        cleaned_course = _clean_local_text(course_name, 80)
        _reject_cjk(cleaned_course, "course_name")
        ctx.course_name = cleaned_course
    if register_number is not None:
        if not 1 <= register_number <= 60:
            raise ValueError(
                f"`register_number` = {register_number} non valido: usa il numero "
                "di elenco reale tra 1 e 60."
            )
        ctx.register_number = register_number
    if custom_notes.strip():
        ctx.custom_notes = _clean_local_text(custom_notes, 1500)
    ctx.school_year = current_school_year_str()

    saved = _store.save_student_context(ctx)
    return {
        "status": "saved",
        "student_context": _context_out(saved),
    }


async def save_class_timetable(
    school_class: str,
    schedule: dict[str, list[dict[str, Any]]],
    valid_from: str = "",
) -> dict[str, Any]:
    """Valida e salva **solo in locale** l'orario settimanale della classe (`school_class`, es. '5AI'),
    così non è necessario rileggere il PDF/Excel dell'orario a ogni sessione.
    L'orario resta un file privato sul dispositivo: non viene mai inviato al registro scolastico.
    `schedule` deve essere un dizionario: giorno ('Lunedi'..'Sabato') -> lista di slot
    `{"hour": 1, "start_time": "08:00", "end_time": "09:00", "subject": "Informatica", "room": "Lab 2", "teacher": "Rossi"}`.
    """
    _ensure_writable()
    normalized_class = _normalize_class_code(school_class)
    ctx = _store.load_student_context()
    if ctx.school_class and not _classes_compatible(ctx.school_class, normalized_class):
        raise ValueError(
            f"Stai salvando l'orario di '{normalized_class}' ma la classe salvata è "
            f"'{ctx.school_class}'. Se hai davvero cambiato classe, chiama prima "
            "`update_student_context(school_class=...)`, poi riprova."
        )

    parsed_schedule: dict[str, list[TimetableSlot]] = {}
    for raw_day, slots in schedule.items():
        day = _normalize_day(raw_day)
        if day in parsed_schedule:
            raise ValueError(f"Giorno duplicato: '{raw_day}' compare più volte nell'orario.")
        parsed_slots: list[TimetableSlot] = []
        seen_hours: set[int] = set()
        for raw_slot in slots:
            slot = TimetableSlot.model_validate(raw_slot)
            slot.subject = _clean_local_text(slot.subject, 120)
            slot.room = _clean_local_text(slot.room, 80)
            slot.teacher = _clean_local_text(slot.teacher, 80)
            _reject_cjk(slot.subject, f"{day}.subject")
            _reject_cjk(slot.room, f"{day}.room")
            _reject_cjk(slot.teacher, f"{day}.teacher")
            if not slot.subject:
                raise ValueError(f"Materia mancante o vuota in {day}, ora {slot.hour}.")
            if slot.hour in seen_hours:
                raise ValueError(
                    f"Ora {slot.hour} duplicata in {day}: ogni ora può comparire una sola volta."
                )
            seen_hours.add(slot.hour)
            slot.start_time = _validate_hhmm(slot.start_time, f"{day}.start_time")
            slot.end_time = _validate_hhmm(slot.end_time, f"{day}.end_time")
            if slot.start_time and slot.end_time and slot.start_time >= slot.end_time:
                raise ValueError(
                    f"In {day}, ora {slot.hour}: start_time ({slot.start_time}) deve essere "
                    f"precedente a end_time ({slot.end_time})."
                )
            parsed_slots.append(slot)
        if parsed_slots:
            parsed_schedule[day] = sorted(parsed_slots, key=lambda s: s.hour)

    if not parsed_schedule:
        raise ValueError("L'orario è vuoto: passa almeno un giorno con una materia.")

    clean_valid_from = _clean_local_text(valid_from, 40)
    if clean_valid_from:
        try:
            clean_valid_from = date.fromisoformat(clean_valid_from).isoformat()
        except ValueError:
            _reject_cjk(clean_valid_from, "valid_from")
    else:
        clean_valid_from = today_rome_str()

    timetable = ClassTimetable(
        school_class=normalized_class,
        valid_from=clean_valid_from,
        schedule=parsed_schedule,
    )
    if not ctx.school_class or len(timetable.school_class) > len(ctx.school_class):
        ctx.school_class = timetable.school_class
    ctx.timetable = timetable
    _store.save_student_context(ctx)
    return {
        "status": "timetable_saved",
        "storage": "local_only",
        "note": (
            "Orario salvato solo sul dispositivo: non viene inviato al registro "
            "scolastico né a Spaggiari."
        ),
        "timetable": timetable.model_dump(),
    }


async def get_homework(days_ahead: int = 7) -> dict[str, Any]:
    """Restituisce i compiti, le verifiche e le interrogazioni (ClasseViva + eventuale Google Classroom
    OAuth2 / iCal `.ics`) da oggi fino a `days_ahead` giorni in avanti (max 60).
    `items[].kind` distingue 'homework' (compito), 'exam' (verifica/interrogazione) e 'assignment' (Classroom).
    Nota: il feed `.ics` di Google Classroom include solo i compiti dotati di data di scadenza;
    avvisi sullo Stream o materiali senza scadenza non compaiono nel feed `.ics`.
    Se una fonte (ClasseViva o Classroom) non risponde, il risultato resta parziale e `warnings` spiega cosa manca.
    """
    _ensure_writable()
    days = max(1, min(60, days_ahead))
    start = now_rome().strftime("%Y-%m-%d")
    end = (now_rome() + timedelta(days=days)).strftime("%Y-%m-%d")

    cvv_items = await _client.get_agenda(start, end)
    homework = [it.model_dump() for it in cvv_items if it.kind in ("homework", "exam")]

    warnings: list[str] = []
    try:
        creds: StoredCredentials | None = _store.load()
    except Exception:  # noqa: BLE001 - no stored credentials: Classroom is simply not configured
        creds = None
    try:
        if creds is not None and (creds.classroom_ical_url or creds.google_access_token):
            classroom = await fetch_classroom_tasks(
                ical_url=creds.classroom_ical_url,
                google_access_token=creds.google_access_token,
                start_date=start,
                end_date=end,
            )
            homework.extend([it.model_dump() for it in classroom.items])
            warnings.extend(classroom.warnings)
    except Exception as exc:  # noqa: BLE001 - degraded mode, never leak secret URL/token
        logger.warning("Classroom non disponibile: %s", type(exc).__name__)
        warnings.append("Google Classroom non disponibile; risultati ClasseViva only.")

    homework.sort(key=lambda x: x["date"])
    breakdown = {"homework": 0, "exam": 0, "assignment": 0, "note": 0}
    for item in homework:
        kind = item.get("kind", "")
        breakdown[kind] = breakdown.get(kind, 0) + 1
    summary = (
        f"{len(homework)} impegni nei prossimi {days} giorni: "
        f"{breakdown['exam']} verifiche/interrogazioni, {breakdown['homework']} compiti"
    )
    if breakdown["assignment"]:
        summary += f", {breakdown['assignment']} consegne Classroom"
    summary += "."
    if warnings:
        summary += " Attenzione: " + " ".join(warnings)
    return {
        "today": start,
        "range": {"start_date": start, "end_date": end},
        "count": len(homework),
        "breakdown": breakdown,
        "summary": summary,
        "items": homework,
        "warnings": warnings,
    }


async def get_agenda(
    start_date: str = "",
    end_date: str = "",
    kind: str = "all",
) -> dict[str, Any]:
    """Restituisce tutti gli eventi dell'agenda scolastica (compiti, verifiche, interrogazioni, annotazioni)
    tra `start_date` e `end_date` (YYYY-MM-DD). Se omessi, copre i prossimi 14 giorni.
    `kind` può essere: 'all', 'homework', 'exam', 'note' (accetta anche 'verifiche', 'compiti', 'interrogazioni').
    `events[].subject` può essere vuoto se Spaggiari non lo fornisce: in quel caso il docente è in `author`.
    """
    _ensure_writable()
    default_end = (now_rome() + timedelta(days=14)).strftime("%Y-%m-%d")
    start, warn_start = _validated_date(start_date, today_rome_str(), "start_date")
    end, warn_end = _validated_date(end_date, default_end, "end_date")
    normalized_kind, warn_kind = _normalize_kind(kind)
    warnings = [w for w in (warn_start, warn_end, warn_kind) if w]

    items = await _client.get_agenda(start, end)
    if normalized_kind != "all":
        items = [it for it in items if it.kind == normalized_kind]

    return {
        "today": today_rome_str(),
        "range": {"start_date": start, "end_date": end},
        "kind_filter": normalized_kind,
        "count": len(items),
        "events": [it.model_dump() for it in items],
        "warnings": warnings,
    }


async def get_lessons(
    start_date: str = "",
    end_date: str = "",
    subject: str = "",
) -> dict[str, Any]:
    """Restituisce le lezioni svolte e gli ARGOMENTI spiegati dai professori in classe.
    Utile per capire cosa ripassare prima di una verifica o cosa era stato spiegato prima di un voto insufficiente.
    Se `start_date` e `end_date` sono vuoti, mostra gli ultimi 14 giorni.
    """
    _ensure_writable()
    default_start = (now_rome() - timedelta(days=14)).strftime("%Y-%m-%d")
    start, warn_start = _validated_date(start_date, default_start, "start_date")
    end, warn_end = _validated_date(end_date, today_rome_str(), "end_date")
    warnings = [w for w in (warn_start, warn_end) if w]

    lessons = await _client.get_lessons(start, end, subject_filter=subject.strip() or None)
    if subject.strip() and not lessons:
        warnings.append(
            f"Nessuna lezione trovata per il filtro '{subject}' nel periodo indicato: "
            "controlla il nome della materia o allarga le date."
        )
    return {
        "today": today_rome_str(),
        "range": {"start_date": start, "end_date": end},
        "subject_filter": subject or None,
        "count": len(lessons),
        "lessons": [ls.model_dump() for ls in lessons],
        "warnings": warnings,
    }


async def get_grades(subject: str = "", period: str = "") -> dict[str, Any]:
    """[DATI PERSONALI] Restituisce i voti dello studente, la media ponderata per ciascuna materia
    e l'elenco delle materie attualmente insufficienti (< 6.0).
    `overall_average` è la media ponderata su TUTTI i voti (non la media delle medie) e vale `null`
    se non ci sono voti: non interpretarlo come 0.
    `subject` filtra per nome materia (sottostringa, case-insensitive); `period` per etichetta del periodo.
    Disabilitato automaticamente se CLASSEVIVA_READ_ONLY_MODE=true.
    """
    _ensure_writable()

    grades = await _client.get_grades(subject_filter=subject.strip() or None)
    if period.strip():
        grades = [g for g in grades if period.lower() in g.period.lower()]

    warnings: list[str] = []
    if not grades:
        if subject.strip():
            warnings.append(f"Nessun voto trovato per la materia '{subject}'.")
        elif period.strip():
            warnings.append(f"Nessun voto trovato per il periodo '{period}'.")
        else:
            warnings.append("Nessun voto disponibile sul registro.")

    by_subject: dict[str, list[tuple[float, float]]] = {}
    for g in grades:
        if g.value is not None and g.weight > 0:
            by_subject.setdefault(g.subject, []).append((g.value, g.weight))

    subject_averages: dict[str, float] = {}
    insufficiencies: list[dict[str, Any]] = []
    for subj, pairs in sorted(by_subject.items()):
        w_sum = sum(v * w for v, w in pairs)
        w_tot = sum(w for _, w in pairs)
        avg = round(w_sum / w_tot, 2) if w_tot > 0 else 0.0
        subject_averages[subj] = avg
        if avg < 6.0:
            insufficiencies.append({"subject": subj, "average": avg, "grades_count": len(pairs)})

    all_pairs = [p for pairs in by_subject.values() for p in pairs]
    overall_avg = (
        round(sum(v * w for v, w in all_pairs) / sum(w for _, w in all_pairs), 2)
        if all_pairs
        else None
    )

    if overall_avg is None:
        summary = "Nessun voto numerico disponibile."
    else:
        summary = (
            f"Media ponderata {overall_avg} su {len(grades)} voti; "
            f"{len(insufficiencies)} materie insufficienti."
        )
    if warnings:
        summary += " " + " ".join(warnings)

    return {
        "today": today_rome_str(),
        "overall_average": overall_avg,
        "average_method": "media ponderata su tutti i voti (weight), non media delle medie per materia",
        "subject_averages": subject_averages,
        "insufficiencies": insufficiencies,
        "grades_count": len(grades),
        "summary": summary,
        "grades": [g.model_dump() for g in grades],
        "warnings": warnings,
    }


async def calculate_grade_target(subject: str, target_average: float = 6.0) -> dict[str, Any]:
    """[DATI PERSONALI] Calcola matematicamente quale voto serve nella prossima verifica di `subject`
    per raggiungere la media `target_average` (default 6.0).
    Se la materia non ha voti numerici, `required_next_grade` vale `null` e `found` è `false`:
    non è un voto richiesto, è l'assenza di dati.
    """
    _ensure_writable()

    grades = await _client.get_grades(subject_filter=subject.strip())
    numeric = [g for g in grades if g.value is not None and g.weight > 0]
    if not numeric:
        return {
            "subject": subject,
            "found": False,
            "grades_count": 0,
            "current_average": None,
            "target_average": target_average,
            "required_next_grade": None,
            "required_across_two_exams": None,
            "achievable_with_one_exam": None,
            "advice": f"Nessun voto numerico presente in {subject}: impossibile calcolare il voto necessario.",
        }

    w_sum = sum((g.value or 0.0) * g.weight for g in numeric)
    w_tot = sum(g.weight for g in numeric)
    current_avg = round(w_sum / w_tot, 2)
    needed_1 = round(target_average * (w_tot + 1.0) - w_sum, 2)
    needed_2 = round((target_average * (w_tot + 2.0) - w_sum) / 2.0, 2)

    if needed_1 <= 0:
        advice = f"Obiettivo {target_average} già superato (media attuale: {current_avg})."
    elif needed_1 > 10.0:
        advice = (
            f"Con 1 sola verifica servirebbe {needed_1} (impossibile). "
            f"Ti servono 2 verifiche con media di almeno {needed_2} ciascuna."
        )
    else:
        advice = f"Nella prossima verifica di {subject} ti serve almeno {needed_1} per arrivare a {target_average}."

    return {
        "subject": numeric[0].subject,
        "found": True,
        "grades_count": len(numeric),
        "current_average": current_avg,
        "target_average": target_average,
        "required_next_grade": max(0.0, needed_1),
        "required_across_two_exams": max(0.0, needed_2),
        "achievable_with_one_exam": needed_1 <= 10.0,
        "max_grade": 10.0,
        "advice": advice,
    }


async def get_absences() -> dict[str, Any]:
    """[DATI PERSONALI] Restituisce assenze, ritardi e uscite anticipate, evidenziando quelle da giustificare.
    `total_events` conta TUTTI gli eventi (giustificati e non): non usarlo come numero di assenze da giustificare.
    Il numero da giustificare è `unjustified_count` (0 = nessuna).
    """
    _ensure_writable()

    items = await _client.get_absences()
    unjustified = [it.model_dump() for it in items if not it.is_justified]
    breakdown: dict[str, int] = {}
    for item in items:
        breakdown[item.kind] = breakdown.get(item.kind, 0) + 1
    if not items:
        summary = "Nessun evento registrato (assenze, ritardi o uscite anticipate)."
    elif not unjustified:
        summary = f"Nessun evento da giustificare su {len(items)} eventi totali."
    else:
        summary = f"{len(unjustified)} eventi da giustificare su {len(items)} totali."
    return {
        "today": today_rome_str(),
        "total_events": len(items),
        "justified_count": len(items) - len(unjustified),
        "unjustified_count": len(unjustified),
        "breakdown_by_kind": breakdown,
        "summary": summary,
        "unjustified": unjustified,
        "events": [it.model_dump() for it in items],
    }


async def get_snapshot(days_ahead: int = 7) -> dict[str, Any]:
    """[DATI PERSONALI] Restituisce in **una sola chiamata** lo stato completo dello studente:
    profilo, contesto, materie, orario, voti con medie, assenze da giustificare, compiti e agenda.

    Pensato per i workflow avanzati: invece di 6-8 chiamate separate (e altrettante
    richieste di rete) l'agente ne fa una sola, e le 5 richieste HTTP partono in
    parallelo. `days_ahead` (1-60, default 7) delimita compiti e agenda.
    In modalità read-only il tool non viene registrato.
    """
    days = max(1, min(60, days_ahead))
    start = now_rome().strftime("%Y-%m-%d")
    end = (now_rome() + timedelta(days=days)).strftime("%Y-%m-%d")

    context, grades, absences, homework, agenda = await asyncio.gather(
        get_today_context(),
        get_grades(),
        get_absences(),
        get_homework(days_ahead=days),
        get_agenda(start_date=start, end_date=end),
    )
    all_warnings: list[str] = []
    for section in (context, grades, homework, agenda):
        for warning in section.get("warnings", []):
            if warning not in all_warnings:
                all_warnings.append(warning)
    return {
        "today": start,
        "range": {"start_date": start, "end_date": end},
        "profile": context.get("profile"),
        "student_context": context.get("student_context"),
        "subjects_and_teachers": context.get("subjects_and_teachers"),
        "grades": {
            "overall_average": grades["overall_average"],
            "average_method": grades["average_method"],
            "subject_averages": grades["subject_averages"],
            "insufficiencies": grades["insufficiencies"],
            "grades_count": grades["grades_count"],
            "summary": grades["summary"],
            "warnings": grades["warnings"],
            "grades": grades["grades"],
        },
        "absences": {
            "total_events": absences["total_events"],
            "justified_count": absences["justified_count"],
            "unjustified_count": absences["unjustified_count"],
            "breakdown_by_kind": absences["breakdown_by_kind"],
            "summary": absences["summary"],
            "unjustified": absences["unjustified"],
        },
        "homework": {
            "count": homework["count"],
            "breakdown": homework["breakdown"],
            "summary": homework["summary"],
            "warnings": homework["warnings"],
            "also_in_agenda": True,
            "items": homework["items"],
        },
        "agenda": {
            "count": agenda["count"],
            "includes_homework": True,
            "warnings": agenda["warnings"],
            "events": agenda["events"],
        },
        "warnings": all_warnings,
    }


@mcp.tool()
async def list_notices(
    query: str = "",
    unread_only: bool = False,
    limit: int = 15,
) -> dict[str, Any]:
    """Elenca e filtra le circolari della bacheca scolastica senza segnarle come lette su Spaggiari.
    Non nasconde le circolari già viste: le mostra tutte contrassegnandole con `status_label`:
    '🆕 NUOVA' (mai mostrata), '👀 GIÀ MOSTRATA ALL'AI' (vista in una lista precedente, non letta sul registro),
    '📌 GIÀ LETTA SUL REGISTRO' (aperta ufficialmente su Spaggiari).
    `count` è il numero restituito, non il totale: leggi `total_available` e `truncated` per sapere quante ce ne sono.
    Usa `query` per cercare parole chiave nel titolo/categoria (es. 'gita', 'orario', 'sciopero', '5AI').
    """
    notices = await _client.list_notices(
        query=query.strip() or None,
        unread_only=unread_only,
    )
    capped = notices[: max(1, min(50, limit))]

    ctx = _store.load_student_context()
    seen_set = set(ctx.seen_notice_ids)
    new_count = 0
    already_seen_count = 0
    read_count = 0
    for n in capped:
        n.seen_by_agent = (n.id in seen_set) or n.is_read
        if n.is_read:
            n.status_label = "📌 GIÀ LETTA SUL REGISTRO"
            read_count += 1
        elif n.seen_by_agent:
            n.status_label = "👀 GIÀ MOSTRATA ALL'AI"
            already_seen_count += 1
        else:
            n.status_label = "🆕 NUOVA"
            new_count += 1

    # Mark returned IDs as seen for future diffing (capped at max 200 FIFO).
    # In read-only mode we must not mutate local state.
    if not READ_ONLY_MODE:
        _store.mark_notices_seen([n.id for n in capped])

    total_available = len(notices)
    truncated = len(capped) < total_available
    unread_available = sum(1 for n in notices if not n.is_read)
    summary = (
        f"{unread_available} circolari non lette sul registro su {total_available} totali"
        + (f"; ne sono mostrate {len(capped)}" if truncated else "")
        + "."
    )
    return {
        "today": today_rome_str(),
        "query": query or None,
        "unread_only": unread_only,
        "count": len(capped),
        "total_available": total_available,
        "truncated": truncated,
        "unread_available": unread_available,
        "new_count": new_count,
        "already_seen_count": already_seen_count,
        "read_count": read_count,
        "summary": summary,
        "read_only_mode": READ_ONLY_MODE,
        "notices": [n.model_dump() for n in capped],
    }


@mcp.tool()
async def read_notice_pdf(
    notice_id: str,
    event_code: str = "CF",
    attach_num: int | None = None,
    attachment_index: int | None = None,
    filter_keyword: str = "",
    save_pdf: bool = True,
    allow_mark_as_read: bool = False,
) -> dict[str, Any]:
    """Scarica ed estrae il testo di una circolare e dei suoi allegati (.pdf, .docx, .xlsx, .zip) dalla bacheca:
    - Se `filter_keyword` è specificato (o se è salvata la classe dello studente in `student_context.json`, es. '5AI'),
      usa una regex tollerante (`5AI`, `5^AI`, `5 A INF`, `V AI`) per estrarre dai PDF multi-pagina o dai tabelloni Excel
      solo le pagine o le colonne della classe, riducendo il consumo di token fino al 90%.
      Nei tabelloni orario che impilano più classi nella stessa pagina ritaglia la sola griglia della classe
      richiesta (intestazione tipo `5 AI - Settimana …`), così non restituisce docenti e aule di altre classi.
    - Se un allegato PDF è una scansione senza testo selezionabile (< 50 caratteri), renderizza automaticamente
      le pagine a 150 DPI in immagini PNG (`rendered_page_images`) così l'agent può leggerle tramite Vision.
    - `attachment_index` (0-based) permette di selezionare un singolo allegato se la circolare ne ha più d'uno.
    - ATTENZIONE: Spaggiari restituisce testo e allegati completi solo dopo aver segnato la circolare come letta.
      Il default è `allow_mark_as_read=False`: se la circolare è nuova il tool restituisce solo i metadati senza
      modificare lo stato remoto. Passa `allow_mark_as_read=True` solo dopo aver avvisato lo studente.
      In modalità read-only il valore viene forzato a False.
    """
    ctx = _store.load_student_context()
    effective_filter = filter_keyword.strip() or ctx.school_class
    effective_allow = allow_mark_as_read and not READ_ONLY_MODE
    effective_save = save_pdf and not READ_ONLY_MODE

    raw = await _client.read_notice_content(
        event_code=event_code.strip() or "CF",
        pub_id=str(notice_id).strip(),
        attach_num=attach_num,
        attachment_index=attachment_index,
        allow_mark_as_read=effective_allow,
    )

    base_result: dict[str, Any] = {
        "notice_id": str(notice_id),
        "event_code": event_code,
        "notice_title": raw.get("notice_title") or None,
        "attachments_total": int(raw.get("attachments_total", 0)),
        "filter_keyword_used": effective_filter or None,
        "read_only_mode": READ_ONLY_MODE,
        "notice_marked_as_read": bool(raw.get("marked_as_read", False)),
        "notice_was_unread": bool(raw.get("was_unread", False)),
        "notice_read_state_known": bool(raw.get("read_state_known", False)),
        "citation": f"Circolare ID #{notice_id} (Bacheca ClasseViva)",
    }

    if raw.get("skipped"):
        if base_result["notice_read_state_known"]:
            skipped_text = (
                "La circolare non è ancora stata aperta su Spaggiari. Per scaricare testo integrale e allegati "
                "serve contrassegnarla come letta: richiama il tool con `allow_mark_as_read=True` dopo aver "
                "avvisato lo studente. In modalità read-only questa operazione non è consentita."
            )
        else:
            skipped_text = (
                "Non è stato possibile verificare se la circolare è già stata letta (elenco della bacheca "
                "non raggiungibile o circolare non presente). Per sicurezza non è stato segnato nulla come "
                "letto: riprova, oppure richiama il tool con `allow_mark_as_read=True` dopo aver avvisato "
                "lo studente. In modalità read-only questa operazione non è consentita."
            )
        base_result.update(
            {
                "attachments_count": 0,
                "text": skipped_text,
                "is_scanned_image_pdf": False,
                "rendered_page_images": [],
                "saved_pdf_path": None,
                "saved_files": [],
            }
        )
        return base_result

    attachments: list[dict[str, Any]] = raw.get("attachments") or []
    if not attachments and raw.get("pdf_bytes"):
        attachments = [{"attach_num": attach_num or 1, "file_name": "", "bytes": raw["pdf_bytes"]}]

    extracted_parts: list[str] = []
    if raw.get("text_body"):
        extracted_parts.append(str(raw["text_body"]))

    saved_files: list[str] = []
    rendered_images: list[str] = []
    out_dir = _store.data_dir / "attachments"
    has_pdf_bytes = False
    pdf_text_len = 0

    for att in attachments:
        b: bytes = att.get("bytes") or b""
        fname: str = att.get("file_name") or ""
        if not b:
            continue

        if b.startswith(b"%PDF") or fname.lower().endswith(".pdf"):
            has_pdf_bytes = True
            txt, total_pages, included_pages = extract_pdf_text(b, filter_keyword=effective_filter)
            pdf_text_len += len(txt.strip())
            if len(txt.strip()) < 50 and total_pages > 0 and not READ_ONLY_MODE:
                imgs = render_scanned_pdf_pages(b, notice_id, out_dir)
                rendered_images.extend(imgs)
            if txt:
                filter_note = (
                    f" (Pagine estratte {included_pages} su {total_pages} per filtro '{effective_filter}')"
                    if effective_filter and len(included_pages) < total_pages
                    else ""
                )
                header = f"--- Allegato PDF: {fname or notice_id}{filter_note} ---\n"
                extracted_parts.append(f"{header}{txt}")
        else:
            txt = extract_attachment_text(b, file_name=fname, filter_keyword=effective_filter)
            if txt:
                header = f"--- Allegato: {fname} ---\n" if fname else ""
                extracted_parts.append(f"{header}{txt}")

        if effective_save:
            p = save_attachment_file(b, notice_id, out_dir, file_name=fname)
            saved_files.append(str(p))

    combined_text = "\n\n".join(extracted_parts).strip()
    is_scanned_image_pdf = bool(has_pdf_bytes and pdf_text_len < 50)
    if not READ_ONLY_MODE:
        _store.mark_notices_seen([str(notice_id)])

    first_pdf = next((p for p in saved_files if p.lower().endswith(".pdf")), None)
    base_result.update(
        {
            "attachments_count": len(attachments),
            "text": combined_text
            or (
                "PDF scansionato come immagine (meno di 50 caratteri di testo selezionabile). "
                "Ispeziona i file PNG in `rendered_page_images` con Vision."
            ),
            "is_scanned_image_pdf": is_scanned_image_pdf,
            "rendered_page_images": rendered_images,
            "saved_pdf_path": first_pdf,
            "saved_files": saved_files,
        }
    )
    return base_result


def setup() -> str:
    """Configura o aggiorna la classe frequentata (es. 5AI), l'indirizzo, il numero sul registro e l'orario."""
    return (
        "Aiutami a configurare il mio profilo studente su ClasseViva MCP:\n"
        "1) Chiama `get_today_context` per vedere cosa è già configurato.\n"
        "2) Chiedimi (se non te l'ho già scritto qui) la mia classe/sezione (es. 5AI), l'indirizzo di studio e il mio numero sul registro, "
        "e salvali con `update_student_context`.\n"
        "3) Cerca con `list_notices(query='orario')` l'ultima circolare dell'orario, estraila filtrando per la mia classe con `read_notice_pdf`, "
        "chiedendomi conferma prima di usare `allow_mark_as_read=True`, e salva il mio orario settimanale con `save_class_timetable`.\n"
        "4) Per il link segreto .ics di Google Classroom, ricordami di eseguire `classeviva-mcp set-classroom-url` nel terminale."
    )


def daily_brief() -> str:
    """Genera un brief giornaliero completo di compiti, verifiche e circolari importanti."""
    return (
        "Chiama `get_today_context`, `get_homework(days_ahead=7)` e `list_notices(limit=10)`. "
        "Organizza un riepilogo diviso in: 1) Materie in orario e compiti/verifiche per domani e prossimi giorni in ordine di urgenza, "
        "2) Circolari importanti per la mia classe (citando l'ID della circolare)."
    )


def recovery_plan(subject: str = "") -> str:
    """Crea un piano strategico per recuperare le insufficienze incrociando voti, argomenti spiegati e prossime verifiche."""
    subj_hint = f" per la materia {subject}" if subject else " per tutte le materie insufficienti"
    return (
        f"Voglio preparare un piano di recupero{subj_hint}. "
        "1) Chiama `get_grades` per individuare i voti negativi e le date in cui li ho presi. "
        "2) Chiama `get_lessons` nelle settimane precedenti a quei voti e fino a oggi per leggere gli argomenti esatti spiegati dal prof. "
        "3) Chiama `get_agenda` per vedere quando c'è la prossima verifica o interrogazione fissata. "
        "4) Chiama `calculate_grade_target` e dammi una scaletta di studio giorno per giorno."
    )


def notices_brief() -> str:
    """Brief read-only delle circolari pubbliche della bacheca."""
    return (
        "Chiama `list_notices(limit=15)` e riassumi le circolari più importanti per la mia classe, "
        "citando l'ID. Se una circolare è nuova e servono gli allegati, chiedimi conferma prima di usare "
        "`read_notice_pdf(allow_mark_as_read=True)`: l'apertura la segna come letta sul registro."
    )


# --- MCP resources ---------------------------------------------------------
# Resources are the read-only counterpart of tools: the client pulls a
# document addressed by URI, can cache it and reuse it without paying another
# tool call. Useful for the large, stable datasets (grades, lessons, agenda).


def _parse_days(value: str) -> int:
    """Parses a bounded integer path parameter (1-60)."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 7
    return max(1, min(60, days))


def _parse_date(value: str, fallback: str) -> str:
    """Validates a YYYY-MM-DD path parameter, falling back when malformed."""
    clean = (value or "").strip()
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError:
        return fallback


async def _resource_profile() -> dict[str, Any]:
    """Data/ora, profilo, contesto studente, materie e orario in un unico documento."""
    return await get_today_context()


async def _resource_subjects() -> dict[str, Any]:
    """Elenco completo delle materie con i relativi professori."""
    subjects = await _client.get_subjects()
    return {"count": len(subjects), "subjects": [s.model_dump() for s in subjects]}


async def _resource_notices() -> dict[str, Any]:
    """Elenco delle circolari (bacheca) senza segnarle come lette."""
    notices = await _client.list_notices()
    return {"count": len(notices), "notices": [n.model_dump() for n in notices]}


async def _resource_grades() -> dict[str, Any]:
    """Voti, medie per materia e materie insufficienti."""
    return await get_grades()


async def _resource_absences() -> dict[str, Any]:
    """Assenze, ritardi e uscite anticipate, con quelle da giustificare."""
    return await get_absences()


async def _resource_homework(days: str) -> dict[str, Any]:
    """Compiti e consegne dei prossimi `days` giorni (1-60, default 7)."""
    return await get_homework(days_ahead=_parse_days(days))


async def _resource_agenda(start_date: str, end_date: str) -> dict[str, Any]:
    """Eventi dell'agenda (compiti, verifiche, note) nel periodo indicato."""
    return await get_agenda(
        start_date=_parse_date(start_date, today_rome_str()),
        end_date=_parse_date(end_date, ""),
    )


async def _resource_lessons(start_date: str, end_date: str) -> dict[str, Any]:
    """Lezioni svolte e argomenti spiegati nel periodo indicato."""
    return await get_lessons(
        start_date=_parse_date(start_date, ""),
        end_date=_parse_date(end_date, today_rome_str()),
    )


mcp.resource(
    "classeviva://profile",
    name="profile",
    title="Profilo, contesto e orario",
    mime_type="application/json",
)(_resource_profile)
mcp.resource(
    "classeviva://subjects",
    name="subjects",
    title="Materie e professori",
    mime_type="application/json",
)(_resource_subjects)
mcp.resource(
    "classeviva://notices",
    name="notices",
    title="Circolari della bacheca",
    mime_type="application/json",
)(_resource_notices)

if not READ_ONLY_MODE:
    mcp.tool()(update_student_context)
    mcp.tool()(save_class_timetable)
    mcp.tool()(get_homework)
    mcp.tool()(get_agenda)
    mcp.tool()(get_lessons)
    mcp.tool()(get_grades)
    mcp.tool()(calculate_grade_target)
    mcp.tool()(get_absences)
    mcp.tool()(get_snapshot)
    mcp.prompt()(setup)
    mcp.prompt()(daily_brief)
    mcp.prompt()(recovery_plan)
    # Personal resources follow the same rule as the personal tools.
    mcp.resource(
        "classeviva://grades",
        name="grades",
        title="Voti e medie",
        mime_type="application/json",
    )(_resource_grades)
    mcp.resource(
        "classeviva://absences",
        name="absences",
        title="Assenze e ritardi",
        mime_type="application/json",
    )(_resource_absences)
    mcp.resource(
        "classeviva://homework/{days}",
        name="homework",
        title="Compiti e consegne",
        mime_type="application/json",
    )(_resource_homework)
    mcp.resource(
        "classeviva://agenda/{start_date}/{end_date}",
        name="agenda",
        title="Agenda scolastica",
        mime_type="application/json",
    )(_resource_agenda)
    mcp.resource(
        "classeviva://lessons/{start_date}/{end_date}",
        name="lessons",
        title="Lezioni e argomenti",
        mime_type="application/json",
    )(_resource_lessons)
else:
    # In read-only mode only `notices_brief` makes sense: the other prompts
    # drive tools that are intentionally not registered here.
    mcp.prompt()(notices_brief)

# `notices_brief` is useful in personal mode too: it only uses `list_notices`
# and `read_notice_pdf` (always registered) and it is the prompt that asks for
# confirmation before marking a notice as read.
if not READ_ONLY_MODE:
    mcp.prompt()(notices_brief)
