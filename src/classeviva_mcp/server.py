from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[attr-defined,no-redef]

from classeviva_mcp.classroom_ical import fetch_classroom_tasks
from classeviva_mcp.client import SpaggiariClient
from classeviva_mcp.credentials import CredentialStore
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
        "nuovo orario scolastico, salvali subito con `update_student_context` o `save_class_timetable`.\n"
        "4. Il link segreto .ics di Google Classroom NON va incollato nella chat: l'utente deve eseguire "
        "`classeviva-mcp set-classroom-url` nel terminale, così il segreto non passa dal provider AI."
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
    try:
        subjects = await _client.get_subjects()
        subjects_list = [s.model_dump() for s in subjects]
    except Exception:
        pass

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
            "register_detected_class": register_class or None,
            "needs_class_review": needs_review,
            "agent_hint": review_hint,
        },
        "read_only_mode": READ_ONLY_MODE,
        "public_mode_active": READ_ONLY_MODE,  # backward-compatible field
        "profile": _profile_out(profile),
        "student_context": _context_out(student_ctx),
        "subjects_and_teachers": subjects_list,
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
        ctx.school_class = school_class.strip().upper()
    if course_name.strip():
        ctx.course_name = course_name.strip()
    if register_number is not None:
        ctx.register_number = register_number
    if custom_notes.strip():
        ctx.custom_notes = custom_notes.strip()
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
    """Valida con schema Pydantic e salva in locale l'orario settimanale della classe (`school_class`, es. '5AI'),
    così non è necessario rileggere il PDF/Excel dell'orario a ogni sessione.
    `schedule` deve essere un dizionario: giorno ('Lunedi'..'Sabato') -> lista di slot
    `{"hour": 1, "start_time": "08:00", "end_time": "09:00", "subject": "Informatica", "room": "Lab 2", "teacher": "Rossi"}`.
    """
    _ensure_writable()
    parsed_schedule: dict[str, list[TimetableSlot]] = {}
    for day, slots in schedule.items():
        parsed_schedule[day] = [TimetableSlot.model_validate(s) for s in slots]

    timetable = ClassTimetable(
        school_class=school_class.strip().upper(),
        valid_from=valid_from.strip() or today_rome_str(),
        schedule=parsed_schedule,
    )
    ctx = _store.load_student_context()
    if not ctx.school_class:
        ctx.school_class = timetable.school_class
    ctx.timetable = timetable
    _store.save_student_context(ctx)
    return {
        "status": "timetable_saved",
        "timetable": timetable.model_dump(),
    }


async def get_homework(days_ahead: int = 7) -> dict[str, Any]:
    """Restituisce i compiti e le consegne (ClasseViva + eventuale Google Classroom OAuth2 / iCal `.ics`)
    da oggi fino a `days_ahead` giorni in avanti (max 60).
    Nota: il feed `.ics` di Google Classroom include solo i compiti dotati di data di scadenza;
    avvisi sullo Stream o materiali senza scadenza non compaiono nel feed `.ics`.
    """
    _ensure_writable()
    days = max(1, min(60, days_ahead))
    start = now_rome().strftime("%Y-%m-%d")
    end = (now_rome() + timedelta(days=days)).strftime("%Y-%m-%d")

    cvv_items = await _client.get_agenda(start, end)
    homework = [it.model_dump() for it in cvv_items if it.kind in ("homework", "exam")]

    warnings: list[str] = []
    try:
        creds = _store.load()
        if creds.classroom_ical_url or creds.google_access_token:
            gc_items = await fetch_classroom_tasks(
                ical_url=creds.classroom_ical_url,
                google_access_token=creds.google_access_token,
                start_date=start,
                end_date=end,
            )
            homework.extend([it.model_dump() for it in gc_items])
    except Exception as exc:  # noqa: BLE001 - degraded mode, never leak secret URL/token
        logger.warning("Classroom non disponibile: %s", type(exc).__name__)
        warnings.append("Google Classroom non disponibile o feed non valido; risultati ClasseViva only.")

    homework.sort(key=lambda x: x["date"])
    return {
        "today": start,
        "range": {"start_date": start, "end_date": end},
        "count": len(homework),
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
    `kind` può essere: 'all', 'homework', 'exam', 'note'.
    """
    _ensure_writable()
    start = start_date.strip() or today_rome_str()
    end = end_date.strip() or (now_rome() + timedelta(days=14)).strftime("%Y-%m-%d")

    items = await _client.get_agenda(start, end)
    if kind and kind != "all":
        items = [it for it in items if it.kind == kind]

    return {
        "today": today_rome_str(),
        "range": {"start_date": start, "end_date": end},
        "count": len(items),
        "events": [it.model_dump() for it in items],
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
    end = end_date.strip() or today_rome_str()
    start = start_date.strip() or (now_rome() - timedelta(days=14)).strftime("%Y-%m-%d")

    lessons = await _client.get_lessons(start, end, subject_filter=subject.strip() or None)
    return {
        "today": today_rome_str(),
        "range": {"start_date": start, "end_date": end},
        "subject_filter": subject or None,
        "count": len(lessons),
        "lessons": [ls.model_dump() for ls in lessons],
    }


async def get_grades(subject: str = "", period: str = "") -> dict[str, Any]:
    """[DATI PERSONALI] Restituisce i voti dello studente, la media ponderata per ciascuna materia
    e l'elenco delle materie attualmente insufficienti (< 6.0).
    Disabilitato automaticamente se CLASSEVIVA_READ_ONLY_MODE=true.
    """
    _ensure_writable()

    grades = await _client.get_grades(subject_filter=subject.strip() or None)
    if period.strip():
        grades = [g for g in grades if period.lower() in g.period.lower()]

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
        else 0.0
    )

    return {
        "today": today_rome_str(),
        "overall_average": overall_avg,
        "subject_averages": subject_averages,
        "insufficiencies": insufficiencies,
        "grades_count": len(grades),
        "grades": [g.model_dump() for g in grades],
    }


async def calculate_grade_target(subject: str, target_average: float = 6.0) -> dict[str, Any]:
    """[DATI PERSONALI] Calcola matematicamente quale voto serve nella prossima verifica di `subject`
    per raggiungere la media `target_average` (default 6.0).
    """
    _ensure_writable()

    grades = await _client.get_grades(subject_filter=subject.strip())
    numeric = [g for g in grades if g.value is not None and g.weight > 0]
    if not numeric:
        return {
            "subject": subject,
            "current_average": None,
            "target_average": target_average,
            "required_next_grade": target_average,
            "advice": f"Nessun voto numerico presente in {subject}.",
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
        "grades_count": len(numeric),
        "current_average": current_avg,
        "target_average": target_average,
        "required_next_grade": max(0.0, needed_1),
        "required_across_two_exams": max(0.0, needed_2),
        "advice": advice,
    }


async def get_absences() -> dict[str, Any]:
    """[DATI PERSONALI] Restituisce assenze, ritardi e uscite anticipate, evidenziando quelle da giustificare."""
    _ensure_writable()

    items = await _client.get_absences()
    unjustified = [it.model_dump() for it in items if not it.is_justified]
    return {
        "today": today_rome_str(),
        "total_events": len(items),
        "unjustified_count": len(unjustified),
        "unjustified": unjustified,
        "events": [it.model_dump() for it in items],
    }


@mcp.tool()
async def list_notices(
    query: str = "",
    unread_only: bool = False,
    limit: int = 15,
) -> dict[str, Any]:
    """Elenca e filtra le circolari della bacheca scolastica senza segnarle come lette su Spaggiari.
    Non nasconde le circolari già viste in passato: le mostra tutte nell'elenco contrassegnandole con
    `status_label` ('🆕 NUOVA' oppure '📌 GIÀ VISTA'), così lo studente può sempre rivederle se se ne dimentica.
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
    for n in capped:
        already_seen = (n.id in seen_set) or n.is_read
        n.seen_by_agent = already_seen
        n.status_label = "📌 GIÀ VISTA" if already_seen else "🆕 NUOVA"
        if already_seen:
            already_seen_count += 1
        else:
            new_count += 1

    # Mark returned IDs as seen for future diffing (capped at max 200 FIFO).
    # In read-only mode we must not mutate local state.
    if not READ_ONLY_MODE:
        _store.mark_notices_seen([n.id for n in capped])

    return {
        "today": today_rome_str(),
        "query": query or None,
        "count": len(capped),
        "new_count": new_count,
        "already_seen_count": already_seen_count,
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
        "filter_keyword_used": effective_filter or None,
        "read_only_mode": READ_ONLY_MODE,
        "notice_marked_as_read": bool(raw.get("marked_as_read", False)),
        "notice_was_unread": bool(raw.get("was_unread", False)),
        "citation": f"Circolare ID #{notice_id} (Bacheca ClasseViva)",
    }

    if raw.get("skipped"):
        base_result.update(
            {
                "attachments_count": 0,
                "text": (
                    "La circolare non è ancora stata aperta su Spaggiari. Per scaricare testo integrale e allegati "
                    "serve contrassegnarla come letta: richiama il tool con `allow_mark_as_read=True` dopo aver "
                    "avvisato lo studente. In modalità read-only questa operazione non è consentita."
                ),
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
            "saved_pdf_path": saved_files[0] if saved_files else None,
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


if not READ_ONLY_MODE:
    mcp.tool()(update_student_context)
    mcp.tool()(save_class_timetable)
    mcp.tool()(get_homework)
    mcp.tool()(get_agenda)
    mcp.tool()(get_lessons)
    mcp.tool()(get_grades)
    mcp.tool()(calculate_grade_target)
    mcp.tool()(get_absences)
    mcp.prompt()(setup)
    mcp.prompt()(daily_brief)
    mcp.prompt()(recovery_plan)
else:
    mcp.prompt()(notices_brief)
