from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

ROME_TZ = ZoneInfo("Europe/Rome")


def now_rome() -> datetime:
    return datetime.now(ROME_TZ)


def today_rome_str() -> str:
    return now_rome().strftime("%Y-%m-%d")


def current_school_year_str(dt: datetime | None = None) -> str:
    """Restituisce l'anno scolastico corrente (es. '2026/2027') in base alla data italiana (inizio 1 settembre)."""
    d = dt or now_rome()
    start_year = d.year if d.month >= 9 else d.year - 1
    return f"{start_year}/{start_year + 1}"



class StudentProfile(BaseModel):
    ident: str = Field(description="Codice identificativo usato per il login (es. S1234567X o G1234567X)")
    student_id: str = Field(description="ID numerico dello studente usato negli endpoint /students/{id}")
    account_type: str = Field(description="'student' (S...) oppure 'parent' (G...)")
    first_name: str = ""
    last_name: str = ""
    school_class: str = ""
    school_name: str = ""


class SubjectTeacher(BaseModel):
    subject_id: int | str = ""
    subject_name: str
    teachers: list[str] = Field(default_factory=list)


class TimetableSlot(BaseModel):
    hour: int = Field(ge=1, le=12, description="Ora di lezione (1 = prima ora, 2 = seconda ora, ecc.)")
    start_time: str = Field(default="", description="Orario inizio es. '08:00'")
    end_time: str = Field(default="", description="Orario fine es. '09:00'")
    subject: str = Field(description="Materia della lezione")
    room: str = Field(default="", description="Aula o laboratorio (es. 'Lab. Info 2')")
    teacher: str = Field(default="", description="Cognome docente (opzionale)")


class ClassTimetable(BaseModel):
    school_class: str = Field(description="Sigla della classe (es. '5AI')")
    valid_from: str = Field(default="", description="Data di entrata in vigore YYYY-MM-DD o nota (es. 'Definitivo')")
    schedule: dict[str, list[TimetableSlot]] = Field(
        default_factory=dict,
        description="Mappa giorno ('Lunedi', 'Martedi', 'Mercoledi', 'Giovedi', 'Venerdi', 'Sabato') -> lista di TimetableSlot",
    )


class StudentContext(BaseModel):
    school_class: str = Field(default="", description="Sigla classe e sezione (es. '5AI')")
    course_name: str = Field(default="", description="Indirizzo di studio (es. 'Informatica')")
    register_number: int | None = Field(default=None, description="Numero di elenco sul registro di classe")
    custom_notes: str = Field(default="", description="Note aggiuntive o preferenze dello studente (max 1500 caratteri)")
    school_year: str = Field(default="", description="Anno scolastico in cui è stata salvata la classe (es. '2026/2027')")
    timetable: ClassTimetable | None = Field(default=None, description="Orario settimanale salvato in locale")
    seen_notice_ids: list[str] = Field(
        default_factory=list,
        description="Coda FIFO (max 200) degli ID circolari già visti, per segnalarli senza nasconderli",
    )
    updated_at: str = ""


class ProfileOut(BaseModel):
    """Profilo restituito all'agente in modalità personale (mai segreti)."""

    ident: str = ""
    student_id: str = ""
    account_type: str = ""
    first_name: str = ""
    last_name: str = ""
    school_class: str = ""
    school_name: str = ""


class PublicProfileOut(BaseModel):
    """Profilo minimizzato per la modalità read-only."""

    school_class: str = ""
    school_name: str = ""


class StudentContextOut(BaseModel):
    """Contesto studente restituito in modalità personale, senza URL iCal."""

    school_class: str = ""
    course_name: str = ""
    register_number: int | None = None
    custom_notes: str = ""
    school_year: str = ""
    classroom_ical_configured: bool = False
    timetable: ClassTimetable | None = None
    seen_notice_ids: list[str] = Field(default_factory=list)
    updated_at: str = ""


class PublicStudentContextOut(BaseModel):
    """Contesto studente minimizzato per la modalità read-only."""

    school_class: str = ""
    course_name: str = ""
    school_year: str = ""
    classroom_ical_configured: bool = False
    timetable: ClassTimetable | None = None




class AgendaItem(BaseModel):
    id: str
    source: str = Field(description="'classeviva', 'classroom_ical' oppure 'classroom_oauth'")
    kind: str = Field(description="'homework' | 'exam' | 'note' | 'assignment'")
    subject: str = Field(
        description="Materia dell'evento. Vuoto se Spaggiari non la fornisce: il docente è in `author`, non qui."
    )
    title: str
    description: str = ""
    date: str = Field(description="Data dell'evento o scadenza in formato YYYY-MM-DD (Europe/Rome)")
    author: str = ""
    url: str | None = None


class LessonItem(BaseModel):
    id: str
    date: str
    subject: str
    lesson_type: str = ""
    topic: str = Field(description="Argomento spiegato dal docente durante la lezione")
    teacher: str = ""
    hour_position: int = 1


class GradeItem(BaseModel):
    id: str
    subject: str
    value: float | None = Field(description="Valore numerico del voto (None se solo giudizio/annotazione)")
    display_value: str
    weight: float = 1.0
    date: str
    period: str = ""
    category: str = ""
    notes: str = ""


class AbsenceItem(BaseModel):
    id: str
    date: str
    kind: str = Field(description="'absence' (Assenza), 'delay' (Ritardo), 'early_exit' (Uscita anticipata)")
    is_justified: bool = True
    reason: str = ""


class NoticeItem(BaseModel):
    id: str
    event_code: str = "CF"
    title: str
    publish_date: str
    category: str = ""
    is_read: bool = False
    seen_by_agent: bool = False
    status_label: str = "🆕 NUOVA"
    has_attachment: bool = False
    attachment_numbers: list[int] = Field(default_factory=list)
    attachment_filenames: list[str] = Field(default_factory=list)


class DidacticsFile(BaseModel):
    content_id: int | str = Field(description="ID univoco del file/contenuto didattico")
    content_name: str = Field(description="Nome del file (es. 'Esercizi_Capitolo4.pdf')")
    share_date: str = Field(default="", description="Data di condivisione YYYY-MM-DD")
    file_type: str = Field(default="", description="Estensione o tipo del file (es. 'pdf', 'docx')")
    size_bytes: int | None = Field(default=None, description="Dimensione del file in byte se disponibile")


class DidacticsFolder(BaseModel):
    folder_id: int | str = Field(description="ID univoco della cartella didattica")
    folder_name: str = Field(description="Nome della cartella o argomento didattico")
    teacher_name: str = Field(default="", description="Nome del docente proprietario")
    teacher_id: int | str = Field(default="", description="ID del docente")
    last_share_date: str = Field(default="", description="Data di ultima modifica o condivisione YYYY-MM-DD")
    files: list[DidacticsFile] = Field(default_factory=list, description="Elenco dei file contenuti nella cartella")


class CalendarDay(BaseModel):
    date: str = Field(description="Data nel formato YYYY-MM-DD")
    day_of_week: int = Field(description="Giorno della settimana (1=Lunedì, ..., 7=Domenica)")
    status: str = Field(description="Stato del giorno: 'HD' (festivo/vacanza), 'WD'/'SD' (scuola/lezione), 'NW' (non lavorativo)")
    is_holiday: bool = Field(default=False, description="True se il giorno è festivo o vacanza (status == 'HD')")
    description: str = Field(default="", description="Descrizione o causale della festività se disponibile")

    @model_validator(mode="after")
    def _compute_is_holiday(self) -> CalendarDay:
        if self.status == "HD":
            self.is_holiday = True
        return self


class SchoolPeriod(BaseModel):
    period_code: str = Field(description="Codice identificativo del periodo (es. 'Q1', 'Q2', 'T1', 'P1')")
    period_pos: int = Field(default=1, description="Posizione ordinale del periodo scolastico (1, 2, 3...)")
    description: str = Field(default="", description="Descrizione del periodo (es. 'Primo Quadrimestre')")
    is_final: bool = Field(default=False, description="True se il periodo corrisponde allo scrutinio finale")
    date_start: str = Field(default="", description="Data di inizio periodo YYYY-MM-DD")
    date_end: str = Field(default="", description="Data di fine periodo YYYY-MM-DD")


class DisciplinaryNote(BaseModel):
    id: str = Field(description="ID univoco della nota (evtId)")
    kind: str = Field(description="Tipo di nota: 'NTTE' (docente), 'NTCL' (classe), 'NTWN' (richiamo/avvertimento), 'NTST' (sanzione/disciplinare)")
    kind_description: str = Field(default="", description="Descrizione italiana del tipo di nota")
    date: str = Field(description="Data dell'evento YYYY-MM-DD")
    teacher: str = Field(default="", description="Nome del docente autore della nota")
    text: str = Field(default="", description="Corpo testuale completo della nota")
    is_read: bool = Field(default=False, description="True se la nota è stata già vista/letta")


class SchoolDocument(BaseModel):
    hash: str = Field(description="Hash identificativo univoco del documento (usato per il download)")
    title: str = Field(description="Titolo o descrizione del documento (es. 'Scheda di Valutazione 1° Quadrimestre')")
    document_type: str = Field(default="", description="Tipologia documento ('pagella', 'certificato', 'scheda')")
    publish_date: str = Field(default="", description="Data di pubblicazione YYYY-MM-DD")


class SchoolBook(BaseModel):
    isbn: str = Field(description="Codice ISBN-13 del libro di testo")
    title: str = Field(description="Titolo dell'opera")
    authors: str = Field(default="", description="Autori del libro")
    publisher: str = Field(default="", description="Casa editrice (es. Zanichelli, Mondadori)")
    subject: str = Field(default="", description="Materia scolastica associata")
    volume: str = Field(default="", description="Volume o tomo (es. '1', '2', 'U' unico)")
    price: float | None = Field(default=None, description="Prezzo di copertina in Euro")
    to_buy: bool = Field(default=True, description="True se il libro è da acquistare")
    is_recommended: bool = Field(default=False, description="True se il libro è solo consigliato")


