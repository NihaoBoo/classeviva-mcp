from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field

ROME_TZ = ZoneInfo("Europe/Rome")


def now_rome() -> datetime:
    return datetime.now(ROME_TZ)


def today_rome_str() -> str:
    return now_rome().strftime("%Y-%m-%d")


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
    custom_notes: str = Field(default="", description="Note aggiuntive o preferenze dello studente")
    timetable: ClassTimetable | None = Field(default=None, description="Orario settimanale salvato in locale")
    updated_at: str = ""


class AgendaItem(BaseModel):
    id: str
    source: str = Field(description="'classeviva', 'classroom_ical' oppure 'classroom_oauth'")
    kind: str = Field(description="'homework' | 'exam' | 'note' | 'assignment'")
    subject: str
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
    has_attachment: bool = False
    attachment_numbers: list[int] = Field(default_factory=list)
    attachment_filenames: list[str] = Field(default_factory=list)
