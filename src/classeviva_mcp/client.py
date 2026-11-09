from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.models import (
    AbsenceItem,
    AgendaItem,
    CalendarDay,
    DidacticsFile,
    DidacticsFolder,
    DisciplinaryNote,
    GradeItem,
    LessonItem,
    NoticeItem,
    SchoolBook,
    SchoolDocument,
    SchoolPeriod,
    StudentProfile,
    SubjectTeacher,
)
from classeviva_mcp.network import build_async_client, describe_network_error

BASE_URL = "https://web.spaggiari.eu/rest/v1"
DEFAULT_HEADERS = {
    "User-Agent": "CVVS/std/4.2.3 Android/12",
    "Z-Dev-Apikey": "Tg1NWEwNGIgIC0K",
    "Content-Type": "application/json",
}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB per attachment
MAX_NOTICE_TOTAL_BYTES = 40 * 1024 * 1024  # 40 MB across all attachments of one notice
ALLOWED_ATTACHMENT_HOSTS = ("cdndoc.spaggiari.eu",)
CACHE_TTL_SECONDS = 60.0  # short-lived cache for read-only endpoints
# After Spaggiari rejects a password, do not retry the same credentials for a
# while: MCP clients call tools in bursts and every call used to trigger a new
# login, which quickly gets the account temporarily locked by Spaggiari.
AUTH_FAILURE_COOLDOWN_SECONDS = 600.0

logger = logging.getLogger(__name__)


class ClasseVivaError(Exception):
    """Base error for ClasseViva client operations."""


class ClasseVivaAuthError(ClasseVivaError):
    """Spaggiari rejected the credentials (wrong password, wrong user, locked account)."""


def _credentials_fingerprint(creds: StoredCredentials) -> str:
    raw = f"{creds.username}\x00{creds.password}\x00{creds.preferred_profile}".encode()
    return hashlib.sha256(raw).hexdigest()


def _spaggiari_error_text(data: Any, resp: httpx.Response) -> str:
    """Extracts a short, non-sensitive error description from a login response."""
    if isinstance(data, dict):
        parts = [str(data.get(k) or "").strip() for k in ("error", "message", "info")]
        text = " - ".join(p for p in parts if p)
        if text:
            return text[:300]
    return f"HTTP {resp.status_code}"


def explain_login_failure(status_code: int, error_text: str, username: str) -> str:
    """Maps a Spaggiari login rejection to an actionable Italian message."""
    lowered = error_text.lower()
    if "otp" in lowered or "2fa" in lowered:
        return "Spaggiari richiede verifica OTP/2FA per questo account: non supportata dall'API."
    hints: list[str] = []
    if "@" in username:
        hints.append(
            "hai usato un'email: se non è collegata all'account riprova con il codice "
            "utente (S... studente, G... genitore) che trovi nel profilo di ClasseViva"
        )
    hints.append("verifica la password accedendo a https://web.spaggiari.eu dal browser")
    hints.append("se accedi solo con 'Accedi con Google/SPID' imposta prima una password ClasseViva dal portale")
    hints.append("dopo molti tentativi errati Spaggiari blocca l'accesso: attendi 15-30 minuti")
    return (
        f"Credenziali rifiutate da ClasseViva (HTTP {status_code}: {error_text}). "
        "Suggerimenti: " + "; ".join(hints) + "."
    )


class SpaggiariClient:
    """Async client for Spaggiari ClasseViva REST v1 API.
    Supports:
    - Login via personal email, Student code (S...), or Parent code (G...)
    - Automatic profile selection when an email has multiple linked profiles (`choices`)
    - Transparent 1-time re-authentication on HTTP 401/403
    - Exponential backoff on HTTP 429 / 5xx
    """

    def __init__(
        self,
        store: CredentialStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        credentials: StoredCredentials | None = None,
        persist_session: bool = True,
    ) -> None:
        self.store = store or CredentialStore()
        self._external_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self._credentials = credentials
        self._persist_session = persist_session
        self._token: str = ""
        self._ident: str = ""
        self._student_id: str = ""
        self._first_name: str = ""
        self._last_name: str = ""
        self._semaphore = asyncio.Semaphore(2)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._auth_failure: tuple[str, float, str] | None = None
        if credentials is None:
            self._load_cached_session()

    def invalidate_cache(self) -> None:
        """Drops the cached read-only responses (called after any mutation)."""
        self._cache.clear()

    def _cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)

    def _http(self) -> httpx.AsyncClient:
        """Returns the shared HTTP client, creating it on first use.

        Reusing one client keeps the TLS session and the connection pool alive
        across tool calls, which removes a full TLS handshake from every
        request.
        """
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = build_async_client(
                timeout=25.0,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            )
        return self._owned_client

    async def aclose(self) -> None:
        """Closes the internally owned HTTP client (if any)."""
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def _load_cached_session(self) -> None:
        cached = self.store.load_session()
        if cached and cached.get("token") and cached.get("student_id"):
            self._token = str(cached["token"])
            self._ident = str(cached.get("ident", ""))
            self._student_id = str(cached["student_id"])
            self._first_name = str(cached.get("first_name", ""))
            self._last_name = str(cached.get("last_name", ""))

    @staticmethod
    def extract_numeric_id(ident: str) -> str:
        digits = re.sub(r"\D", "", ident or "")
        return digits or ident

    @staticmethod
    def detect_account_type(ident: str) -> str:
        up = (ident or "").upper()
        if up.startswith("G"):
            return "parent"
        return "student"

    async def _raw_post_login(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._http().post(
                f"{BASE_URL}/auth/login",
                headers=DEFAULT_HEADERS,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ClasseVivaError(describe_network_error(exc)) from exc

    @staticmethod
    def _parse_login_json(resp: httpx.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type.lower():
                raise ClasseVivaError(
                    f"Spaggiari ha risposto con una pagina HTML (HTTP {resp.status_code}) invece "
                    "che con JSON: quasi sempre è la pagina di blocco di un filtro web o di un "
                    "captive portal (rete scolastica/Wi-Fi ospiti). Apri web.spaggiari.eu nel "
                    "browser per verificare, oppure usa un'altra rete."
                ) from exc
            raise ClasseVivaError(f"Risposta non valida da Spaggiari (HTTP {resp.status_code}).") from exc
        return data if isinstance(data, dict) else {}

    async def login(self) -> StudentProfile:
        """Logs in using stored credentials (email, S-code, or G-code).
        Automatically resolves multi-profile `choices` if returned by Spaggiari.

        When ``credentials`` were injected at construction time they are used
        instead of the persisted ones, and the session is not written to disk
        unless ``persist_session`` is True. This lets the CLI validate a
        password against Spaggiari *before* storing it.

        A rejected password is remembered for ``AUTH_FAILURE_COOLDOWN_SECONDS``
        so that a burst of tool calls does not lock the account; changing the
        stored credentials (``login`` again) clears the cooldown immediately.
        """
        creds = self._credentials or self.store.load()
        fingerprint = _credentials_fingerprint(creds)
        if self._auth_failure is not None:
            failed_fp, until, message = self._auth_failure
            if failed_fp == fingerprint and time.monotonic() < until:
                raise ClasseVivaAuthError(
                    message + " (Nuovo tentativo sospeso per evitare il blocco dell'account: "
                    "esegui `classeviva-mcp login` per aggiornare le credenziali.)"
                )
            self._auth_failure = None

        payload: dict[str, Any] = {
            "ident": creds.preferred_profile or None,
            "pass": creds.password,
            "uid": creds.username,
        }

        resp = await self._raw_post_login(payload)
        if resp.status_code >= 500:
            raise ClasseVivaError(f"Spaggiari non disponibile (HTTP {resp.status_code}): riprova tra qualche minuto.")
        data = self._parse_login_json(resp)

        # When logging in with a personal email linked to multiple profiles (e.g. G... and S...),
        # Spaggiari may return {"choices": [{"ident": "G1234567X", ...}, {"ident": "S1234567X", ...}]}
        choices = data.get("choices") or []
        if choices and not data.get("token"):
            chosen_ident = creds.preferred_profile
            if not chosen_ident:
                # Prefer S (student) or G (parent) profile automatically
                student_choices = [c for c in choices if str(c.get("ident", "")).upper().startswith(("S", "G"))]
                chosen_ident = (student_choices[0] if student_choices else choices[0]).get("ident", "")
            payload["ident"] = chosen_ident
            resp = await self._raw_post_login(payload)
            data = self._parse_login_json(resp)

        if resp.status_code in (401, 403, 422) or not data.get("token"):
            message = explain_login_failure(resp.status_code, _spaggiari_error_text(data, resp), creds.username)
            self._auth_failure = (
                fingerprint,
                time.monotonic() + AUTH_FAILURE_COOLDOWN_SECONDS,
                message,
            )
            raise ClasseVivaAuthError(message)

        self._token = str(data["token"])
        self._ident = str(data.get("ident") or creds.username)
        self._student_id = self.extract_numeric_id(self._ident)
        self._first_name = str(data.get("firstName", ""))
        self._last_name = str(data.get("lastName", ""))

        if self._persist_session:
            self.store.save_session(
                {
                    "token": self._token,
                    "ident": self._ident,
                    "student_id": self._student_id,
                }
            )

        return StudentProfile(
            ident=self._ident,
            student_id=self._student_id,
            account_type=self.detect_account_type(self._ident),
            first_name=self._first_name,
            last_name=self._last_name,
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        accept_binary: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if not self._token or not self._student_id:
            await self.login()

        async with self._semaphore:
            for attempt in range(3):
                headers = dict(DEFAULT_HEADERS)
                headers["Z-Auth-Token"] = self._token
                if accept_binary:
                    headers.pop("Content-Type", None)

                url = f"{BASE_URL}{endpoint.format(student_id=self._student_id)}"
                try:
                    resp = await self._http().request(method, url, headers=headers, **kwargs)
                except httpx.HTTPError as exc:
                    raise ClasseVivaError(describe_network_error(exc)) from exc

                if 300 <= resp.status_code < 400:
                    raise ClasseVivaError(
                        "Redirect inatteso dall'API Spaggiari: richiesta interrotta per sicurezza."
                    )

                if resp.status_code in (401, 403) and attempt == 0:
                    await self.login()
                    continue

                if resp.status_code in (429, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue

                resp.raise_for_status()
                return resp

        raise ClasseVivaError(f"Richiesta fallita dopo 3 tentativi su {endpoint}")

    @staticmethod
    async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
        content_length = response.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > max_bytes:
            raise ClasseVivaError(
                f"Allegato troppo grande (limite {max_bytes // (1024 * 1024)} MB)."
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ClasseVivaError(
                    f"Allegato troppo grande (limite {max_bytes // (1024 * 1024)} MB)."
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _is_allowed_attachment_host(host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        return host in ALLOWED_ATTACHMENT_HOSTS or host.endswith(".spaggiari.eu")

    async def _download_attachment(
        self,
        event_code: str,
        pub_id: str,
        num: int,
        *,
        max_bytes: int | None = None,
        auto_unlock: bool = False,
    ) -> bytes:
        """Downloads an attachment while streaming, with a hard byte cap.

        The response is never buffered in full before the limit is applied.
        """
        if max_bytes is None:
            max_bytes = MAX_ATTACHMENT_BYTES
        url = (
            f"{BASE_URL}/students/{self._student_id}/noticeboard/attach/"
            f"{event_code}/{pub_id}/{num}"
        )
        headers = dict(DEFAULT_HEADERS)
        headers.pop("Content-Type", None)
        headers["Z-Auth-Token"] = self._token

        client = (
            self._external_client
            if self._external_client is not None
            else self._http()
        )
        try:
            return await self._download_attachment_with_client(
                client, url, headers, max_bytes
            )
        except httpx.HTTPStatusError as exc:
            if auto_unlock and exc.response.status_code == 404:
                logger.info("Allegato %s bloccato (404): sblocco circolare %s...", num, pub_id)
                if await self.mark_notice_read(event_code, pub_id):
                    return await self._download_attachment_with_client(
                        client, url, headers, max_bytes
                    )
            raise

    async def _download_attachment_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        max_bytes: int,
    ) -> bytes:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise ClasseVivaError("Redirect allegato senza header Location.")
                target = urljoin(url, location)
                parsed = urlparse(target)
                if parsed.scheme != "https" or not self._is_allowed_attachment_host(
                    parsed.hostname or ""
                ):
                    raise ClasseVivaError("Redirect allegato verso host non consentito.")
                # The signed CDN URL must be fetched without Spaggiari auth headers.
                async with client.stream("GET", target, headers={}) as cdn_response:
                    cdn_response.raise_for_status()
                    return await self._read_limited(cdn_response, max_bytes)

            response.raise_for_status()
            return await self._read_limited(response, max_bytes)

    async def get_profile(self) -> StudentProfile:
        """Returns student & school profile details from /students/{id}/cards."""
        if not self._token or not self._student_id:
            await self.login()
        cached = self._cache_get("profile")
        if cached is not None:
            # Copy: callers may tweak the returned profile (e.g. fill in the
            # saved class), and we must not mutate the cached instance.
            return cached.model_copy()
        school_class = ""
        school_name = ""
        for ep in ("/students/{student_id}/cards", "/students/{student_id}/card"):
            try:
                resp = await self._request("GET", ep)
                body = resp.json()
                card = body.get("card") or ((body.get("cards") or [{}])[0])
                if not card:
                    continue
                if card.get("usrId"):
                    self._student_id = str(card["usrId"])
                self._first_name = card.get("firstName") or self._first_name
                self._last_name = card.get("lastName") or self._last_name
                school_class = str(card.get("classDesc") or card.get("miurDivisionCode") or "")
                sch_parts = [
                    str(card.get("schName") or "").strip(),
                    str(card.get("schDedication") or "").strip(),
                    f"({card.get('schCity')})" if card.get("schCity") else "",
                ]
                school_name = " ".join(p for p in sch_parts if p).strip()
                if school_name or school_class:
                    break
            except Exception:
                continue

        profile = StudentProfile(
            ident=self._ident,
            student_id=self._student_id,
            account_type=self.detect_account_type(self._ident),
            first_name=self._first_name,
            last_name=self._last_name,
            school_class=school_class,
            school_name=school_name,
        )
        self._cache_set("profile", profile)
        return profile

    async def get_agenda(self, start_date: str, end_date: str) -> list[AgendaItem]:
        """Fetches agenda items (homework, exams, notes) between start_date and end_date (YYYY-MM-DD)."""
        s_clean = start_date.replace("-", "")
        e_clean = end_date.replace("-", "")
        cache_key = f"agenda:{s_clean}:{e_clean}"
        raw_items = self._cache_get(cache_key)
        if raw_items is None:
            raw_items = await self._fetch_json(
                cache_key,
                f"/students/{{student_id}}/agenda/all/{s_clean}/{e_clean}",
                "agenda",
            )

        items: list[AgendaItem] = []
        for ev in raw_items:
            evt_id = str(ev.get("evtId", ""))
            date_str = str(ev.get("evtDatetimeBegin", ""))[:10]
            # Never fall back to the teacher name here: the author is already exposed
            # separately in `author`, and an agent could mistake it for a subject.
            subject = (ev.get("subjectDesc") or "").strip().title()
            notes = (ev.get("notes") or "").strip()
            author = (ev.get("authorName") or "").strip().title()
            code = str(ev.get("evtCode", "AGNT"))

            notes_low = notes.lower()
            if code == "AGHW":
                kind = "homework"
            elif any(w in notes_low for w in ("verifica", "compito in classe", "interrogazione", "test", "esame")):
                kind = "exam"
            else:
                kind = "note"

            items.append(
                AgendaItem(
                    id=f"cvv:{evt_id}",
                    source="classeviva",
                    kind=kind,
                    subject=subject,
                    title=notes[:100] if notes else f"{kind.title()} {subject}".strip(),
                    description=notes[:4000],
                    date=date_str,
                    author=author,
                )
            )
        items.sort(key=lambda x: x.date)
        return items

    async def get_lessons(
        self,
        start_date: str,
        end_date: str,
        subject_filter: str | None = None,
    ) -> list[LessonItem]:
        """Fetches lessons and topics explained in class between start_date and end_date (YYYY-MM-DD)."""
        s_clean = start_date.replace("-", "")
        e_clean = end_date.replace("-", "")
        cache_key = f"lessons:{s_clean}:{e_clean}"
        raw_lessons = self._cache_get(cache_key)
        if raw_lessons is None:
            raw_lessons = await self._fetch_json(
                cache_key,
                f"/students/{{student_id}}/lessons/{s_clean}/{e_clean}",
                "lessons",
            )

        results: list[LessonItem] = []
        for ls in raw_lessons:
            subj = (ls.get("subjectDesc") or "Materia").strip().title()
            if subject_filter and subject_filter.lower() not in subj.lower():
                continue
            topic = (ls.get("lessonArg") or ls.get("lessonType") or "").strip()[:4000]
            results.append(
                LessonItem(
                    id=str(ls.get("evtId", "")),
                    date=str(ls.get("evtDate", ""))[:10],
                    subject=subj,
                    lesson_type=str(ls.get("lessonType", "")).strip(),
                    topic=topic,
                    teacher=(ls.get("authorName") or "").strip().title(),
                    hour_position=int(ls.get("evtHPos") or 1),
                )
            )
        results.sort(key=lambda x: (x.date, x.hour_position))
        return results

    async def _fetch_json(self, cache_key: str, endpoint: str, field: str) -> list[Any]:
        """Fetches a read-only list endpoint and caches it briefly.

        The cache keeps advanced workflows (brief + recovery plan + grades in a
        row) from hitting the Spaggiari API more than once for the same data.
        """
        resp = await self._request("GET", endpoint)
        body = resp.json()
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = body.get(field) or []
        else:
            items = []
        self._cache_set(cache_key, items)
        return items

    async def get_grades(self, subject_filter: str | None = None) -> list[GradeItem]:
        """Fetches student grades."""
        cache_key = "grades"
        cached = self._cache_get(cache_key)
        raw_grades = cached if cached is not None else await self._fetch_json(
            "grades", "/students/{student_id}/grades", "grades"
        )

        results: list[GradeItem] = []
        for g in raw_grades:
            if g.get("canceled"):
                continue
            subj = (g.get("subjectDesc") or "Materia").strip().title()
            if subject_filter and subject_filter.lower() not in subj.lower():
                continue
            dec_val = g.get("decimalValue")
            val = float(dec_val) if dec_val is not None else None
            results.append(
                GradeItem(
                    id=str(g.get("evtId", "")),
                    subject=subj,
                    value=val,
                    display_value=str(g.get("displayValue") or (val if val is not None else "")),
                    weight=float(g.get("weightFactor") or 1.0),
                    date=str(g.get("evtDate", ""))[:10],
                    period=str(g.get("periodDesc", "")).strip(),
                    category=str(g.get("componentDesc", "")).strip(),
                    notes=str(g.get("notesForFamily", "")).strip(),
                )
            )
        results.sort(key=lambda x: x.date, reverse=True)
        return results

    async def get_absences(self) -> list[AbsenceItem]:
        """Fetches absences, delays, and early exits."""
        events = self._cache_get("absences")
        if events is None:
            events = await self._fetch_json(
                "absences", "/students/{student_id}/absences/details", "events"
            )

        code_map = {
            "ABA0": "absence",
            "ABR0": "delay",
            "ABR1": "short_delay",
            "ABU0": "early_exit",
        }
        results: list[AbsenceItem] = []
        for ev in events:
            code = str(ev.get("evtCode", "ABA0"))
            results.append(
                AbsenceItem(
                    id=str(ev.get("evtId", "")),
                    date=str(ev.get("evtDate", ""))[:10],
                    kind=code_map.get(code, code),
                    is_justified=bool(ev.get("isJustified", True)),
                    reason=str(ev.get("justifReasonDesc") or "").strip(),
                )
            )
        results.sort(key=lambda x: x.date, reverse=True)
        return results

    async def list_notices(self, query: str | None = None, unread_only: bool = False) -> list[NoticeItem]:
        """Lists noticeboard circulars (bacheca) without triggering read receipts."""
        items = self._cache_get("noticeboard")
        if items is None:
            items = await self._fetch_json(
                "noticeboard", "/students/{student_id}/noticeboard", "items"
            )

        results: list[NoticeItem] = []
        for it in items:
            pub_id = str(it.get("pubId") or it.get("evtId") or "")
            title = str(it.get("cntTitle") or it.get("title") or "Circolare").strip()
            category = str(it.get("cntCategory") or "").strip()
            is_read = bool(it.get("readStatus", False))

            if unread_only and is_read:
                continue
            if query and query.lower() not in f"{title} {category}".lower():
                continue

            attachments = it.get("attachments") or []
            attach_nums: list[int] = []
            for attachment in attachments:
                try:
                    attach_nums.append(int(attachment.get("attachNum", 101)))
                except (TypeError, ValueError):
                    attach_nums.append(101)
            attach_names = [str(a.get("fileName", "")) for a in attachments if a.get("fileName")]

            results.append(
                NoticeItem(
                    id=pub_id,
                    event_code=str(it.get("evtCode") or "CF"),
                    title=title,
                    publish_date=str(it.get("pubDT") or "")[:10],
                    category=category,
                    is_read=is_read,
                    has_attachment=bool(attachments),
                    attachment_numbers=attach_nums or ([1] if attachments else []),
                    attachment_filenames=attach_names,
                )
            )
        return results

    async def get_subjects(self) -> list[SubjectTeacher]:
        """Fetches the list of school subjects and assigned teachers from /students/{id}/subjects."""
        raw_subjects = self._cache_get("subjects")
        if raw_subjects is None:
            raw_subjects = await self._fetch_json(
                "subjects", "/students/{student_id}/subjects", "subjects"
            )
        results: list[SubjectTeacher] = []
        for s in raw_subjects:
            teachers = [
                str(t.get("teacherName") or "").strip().title()
                for t in (s.get("teachers") or [])
                if t.get("teacherName")
            ]
            results.append(
                SubjectTeacher(
                    subject_id=s.get("id", ""),
                    subject_name=str(s.get("description") or "Materia").strip().title(),
                    teachers=teachers,
                )
            )
        return results

    async def read_notice_content(
        self,
        event_code: str,
        pub_id: str,
        attach_num: int | None = None,
        *,
        attachment_index: int | None = None,
        allow_mark_as_read: bool = False,
    ) -> dict[str, Any]:
        """Reads notice text and downloads attachments.

        Spaggiari only returns the full ``cntText`` and allows attachment downloads
        after the read endpoint is called. If the notice is unread and
        ``allow_mark_as_read`` is False, this method returns metadata only and
        never changes the remote read status.

        ``attachment_index`` is the 0-based index into the sorted attachment list;
        when set, only that attachment is downloaded.
        """
        text_body = ""
        notice_title = ""
        attachments_meta: list[dict[str, Any]] = []
        was_unread = False
        found = False

        try:
            # Always fetched fresh (never from the cache): the read state must
            # be the real one, otherwise we could mark an unread notice as
            # read without consent.
            nb_resp = await self._request("GET", "/students/{student_id}/noticeboard")
            for it in nb_resp.json().get("items") or []:
                if str(it.get("pubId") or it.get("evtId")) == str(pub_id):
                    found = True
                    attachments_meta = it.get("attachments") or []
                    notice_title = str(it.get("cntTitle") or "").strip()
                    was_unread = not bool(it.get("readStatus", False))
                    break
        except Exception:
            pass

        # Without explicit consent we must never reach the `read` endpoint,
        # because that call marks the notice as read on the registry.
        # We only proceed when we positively know the notice is ALREADY read
        # (the call is then a no-op for the remote state). If the listing
        # failed or the notice was not found, the read state is unknown:
        # fail closed and return metadata only.
        if not allow_mark_as_read and not (found and not was_unread):
            return {
                "text_body": notice_title,
                "notice_title": notice_title,
                "pdf_bytes": b"",
                "attachments": [],
                "attachments_total": len(attachments_meta),
                "marked_as_read": False,
                "was_unread": was_unread,
                "read_state_known": found,
                "skipped": True,
            }

        marked_as_read = False
        read_endpoint_called = False
        try:
            read_resp = await self._request(
                "POST",
                f"/students/{{student_id}}/noticeboard/read/{event_code}/{pub_id}/101",
            )
            read_endpoint_called = True
            item_data = read_resp.json().get("item") or {}
            text_body = str(item_data.get("cntText") or item_data.get("title") or "").strip()
            marked_as_read = was_unread
        except Exception:
            pass

        if read_endpoint_called:
            # The remote read state may have changed: drop the cached noticeboard.
            self.invalidate_cache()

        if not text_body:
            text_body = notice_title

        if attach_num is not None:
            nums_to_try = [(attach_num, "")]
        elif attachments_meta:
            # Sort so .pdf attachments are processed first, followed by .docx/others
            sorted_meta = sorted(
                attachments_meta,
                key=lambda a: (0 if str(a.get("fileName", "")).lower().endswith(".pdf") else 1),
            )
            nums_to_try = [
                (int(a.get("attachNum", 1)), str(a.get("fileName", "")))
                for a in sorted_meta
            ]
            if attachment_index is not None:
                if 0 <= attachment_index < len(nums_to_try):
                    nums_to_try = [nums_to_try[attachment_index]]
                else:
                    nums_to_try = []
        elif found:
            nums_to_try = []
        else:
            nums_to_try = [(1, ""), (101, "")]
            if attachment_index is not None and attachment_index != 0:
                nums_to_try = []

        downloaded: list[dict[str, Any]] = []
        total_bytes = 0
        for num, fname in nums_to_try:
            remaining = MAX_NOTICE_TOTAL_BYTES - total_bytes
            if remaining <= 0:
                logger.warning("Limite totale allegati della circolare raggiunto.")
                break
            try:
                attachment_bytes = await self._download_attachment(
                    event_code,
                    pub_id,
                    num,
                    max_bytes=min(MAX_ATTACHMENT_BYTES, remaining),
                )
                if attachment_bytes:
                    downloaded.append(
                        {
                            "attach_num": num,
                            "file_name": fname,
                            "bytes": attachment_bytes,
                        }
                    )
                    total_bytes += len(attachment_bytes)
            except Exception as exc:  # noqa: BLE001 - one bad attachment must not break the notice
                is_404 = (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
                ) or (hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 404)
                if is_404:
                    logger.info("Allegato %s bloccato (404): sblocco circolare %s...", num, pub_id)
                    unlocked = await self.mark_notice_read(event_code, pub_id)
                    if unlocked:
                        marked_as_read = True
                        try:
                            attachment_bytes = await self._download_attachment(
                                event_code,
                                pub_id,
                                num,
                                max_bytes=min(MAX_ATTACHMENT_BYTES, remaining),
                            )
                            if attachment_bytes:
                                downloaded.append(
                                    {
                                        "attach_num": num,
                                        "file_name": fname,
                                        "bytes": attachment_bytes,
                                    }
                                )
                                total_bytes += len(attachment_bytes)
                                continue
                        except Exception as retry_exc:
                            logger.warning(
                                "Allegato non scaricato dopo sblocco (%s): %s",
                                num,
                                type(retry_exc).__name__,
                            )
                            continue
                logger.warning("Allegato non scaricato (%s): %s", num, type(exc).__name__)
                continue

        primary_bytes = downloaded[0]["bytes"] if downloaded else b""
        return {
            "text_body": text_body,
            "notice_title": notice_title,
            "pdf_bytes": primary_bytes,
            "attachments": downloaded,
            "attachments_total": len(attachments_meta),
            "marked_as_read": marked_as_read,
            "was_unread": was_unread,
            "read_state_known": True,
            "skipped": False,
        }

    @staticmethod
    def _parse_content_disposition_filename(headers: httpx.Headers) -> str:
        """Extracts filename from Content-Disposition header (RFC 5987 / 6266 and standard)."""
        cd = headers.get("content-disposition", "")
        if not cd:
            return ""
        m_star = re.search(r"filename\*\s*=\s*UTF-8''([^;\r\n]+)", cd, re.IGNORECASE)
        if m_star:
            return unquote(m_star.group(1)).strip().strip('"')
        m = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', cd, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ""

    async def mark_notice_read(self, event_code: str, pub_id: str) -> bool:
        """Marks a circular notice as read on Spaggiari, unlocking its attachments."""
        try:
            resp = await self._request(
                "POST",
                f"/students/{{student_id}}/noticeboard/read/{event_code}/{pub_id}/101",
            )
            self.invalidate_cache()
            return resp.status_code == 200
        except Exception as exc:
            logger.warning(
                "Impossibile contrassegnare la circolare %s come letta: %s", pub_id, exc
            )
            return False

    async def get_didactics(self) -> list[DidacticsFolder]:
        """Fetches didactics folders and shared teacher files from /students/{id}/didactics."""
        cache_key = "didactics"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        resp = await self._request("GET", "/students/{student_id}/didactics")
        data = resp.json()
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("didactics") or data.get("didacticts") or []
        else:
            raw_items = []

        folders: list[DidacticsFolder] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            # Case 1: item is a teacher object containing a list of 'folders'
            if "folders" in item and isinstance(item["folders"], list):
                t_name = str(item.get("teacherName") or "").strip().title()
                t_id = item.get("teacherId") or ""
                for f in item["folders"]:
                    if not isinstance(f, dict):
                        continue
                    files: list[DidacticsFile] = []
                    for c in (f.get("contents") or f.get("files") or []):
                        if not isinstance(c, dict):
                            continue
                        c_name = str(
                            c.get("contentName") or c.get("name") or c.get("fileName") or ""
                        ).strip()
                        c_id = c.get("contentId") or c.get("id") or ""
                        s_date = str(c.get("lastShareDT") or c.get("shareDate") or "")[:10]
                        ext = c_name.rsplit(".", 1)[-1].lower() if "." in c_name else ""
                        f_type = str(c.get("fileType") or c.get("type") or ext).strip()
                        raw_size = c.get("sizeBytes") or c.get("size")
                        size_bytes: int | None = None
                        if raw_size is not None:
                            try:
                                size_bytes = int(raw_size)
                            except (ValueError, TypeError):
                                size_bytes = None
                        files.append(
                            DidacticsFile(
                                content_id=c_id,
                                content_name=c_name,
                                share_date=s_date,
                                file_type=f_type,
                                size_bytes=size_bytes,
                            )
                        )
                    f_id = f.get("folderId") or f.get("id") or ""
                    f_name = str(f.get("folderName") or f.get("name") or "").strip()
                    f_date = str(f.get("lastShareDT") or f.get("lastShareDate") or "")[:10]
                    folders.append(
                        DidacticsFolder(
                            folder_id=f_id,
                            folder_name=f_name,
                            teacher_name=t_name,
                            teacher_id=t_id,
                            last_share_date=f_date,
                            files=files,
                        )
                    )
            else:
                # Case 2: item is directly a folder object
                f_id = item.get("folderId") or item.get("id") or ""
                f_name = str(item.get("folderName") or item.get("name") or "").strip()
                t_name = str(item.get("teacherName") or "").strip().title()
                t_id = item.get("teacherId") or ""
                f_date = str(item.get("lastShareDT") or item.get("lastShareDate") or "")[:10]
                files = []
                for c in (item.get("contents") or item.get("files") or []):
                    if not isinstance(c, dict):
                        continue
                    c_name = str(
                        c.get("contentName") or c.get("name") or c.get("fileName") or ""
                    ).strip()
                    c_id = c.get("contentId") or c.get("id") or ""
                    s_date = str(c.get("lastShareDT") or c.get("shareDate") or "")[:10]
                    ext = c_name.rsplit(".", 1)[-1].lower() if "." in c_name else ""
                    f_type = str(c.get("fileType") or c.get("type") or ext).strip()
                    raw_size = c.get("sizeBytes") or c.get("size")
                    size_bytes = None
                    if raw_size is not None:
                        try:
                            size_bytes = int(raw_size)
                        except (ValueError, TypeError):
                            size_bytes = None
                    files.append(
                        DidacticsFile(
                            content_id=c_id,
                            content_name=c_name,
                            share_date=s_date,
                            file_type=f_type,
                            size_bytes=size_bytes,
                        )
                    )
                folders.append(
                    DidacticsFolder(
                        folder_id=f_id,
                        folder_name=f_name,
                        teacher_name=t_name,
                        teacher_id=t_id,
                        last_share_date=f_date,
                        files=files,
                    )
                )

        self._cache_set(cache_key, folders)
        return folders

    async def download_didactics_file(
        self,
        content_id: int | str,
        *,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> tuple[bytes, str, str]:
        """Downloads a didactic file with streaming byte cap and redirect validation.

        Returns: (raw_bytes, file_name, content_type)
        """
        if not self._token or not self._student_id:
            await self.login()

        url = f"{BASE_URL}/students/{self._student_id}/didactics/item/{content_id}"
        headers = dict(DEFAULT_HEADERS)
        headers.pop("Content-Type", None)
        headers["Z-Auth-Token"] = self._token

        client = (
            self._external_client
            if self._external_client is not None
            else self._http()
        )
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise ClasseVivaError("Redirect didattica senza header Location.")
                target = urljoin(url, location)
                parsed = urlparse(target)
                if parsed.scheme != "https" or not self._is_allowed_attachment_host(
                    parsed.hostname or ""
                ):
                    raise ClasseVivaError("Redirect didattica verso host non consentito.")
                async with client.stream("GET", target, headers={}) as cdn_response:
                    cdn_response.raise_for_status()
                    data = await self._read_limited(cdn_response, max_bytes)
                    fname = self._parse_content_disposition_filename(cdn_response.headers)
                    if not fname:
                        path_last = parsed.path.rsplit("/", 1)[-1]
                        if "." in path_last:
                            fname = path_last
                    if not fname:
                        fname = self._parse_content_disposition_filename(response.headers)
                    ctype = (
                        cdn_response.headers.get("content-type", "").split(";")[0].strip()
                        or "application/octet-stream"
                    )
                    return data, fname or f"didactic_{content_id}", ctype

            response.raise_for_status()
            data = await self._read_limited(response, max_bytes)
            fname = (
                self._parse_content_disposition_filename(response.headers)
                or f"didactic_{content_id}"
            )
            ctype = (
                response.headers.get("content-type", "").split(";")[0].strip()
                or "application/octet-stream"
            )
            return data, fname, ctype

    async def get_calendar(self) -> list[CalendarDay]:
        """Fetches the school calendar with school days and holidays from /students/{id}/calendar/all."""
        cache_key = "calendar"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        resp = await self._request("GET", "/students/{student_id}/calendar/all")
        data = resp.json()
        if isinstance(data, list):
            raw_days = data
        elif isinstance(data, dict):
            raw_days = data.get("calendar") or []
        else:
            raw_days = []

        days: list[CalendarDay] = []
        for d in raw_days:
            if not isinstance(d, dict):
                continue
            raw_date = str(d.get("dayDate") or d.get("date") or "")
            if len(raw_date) == 8 and raw_date.isdigit():
                date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            else:
                date_str = raw_date[:10]
            if not date_str:
                continue

            try:
                dow = int(d.get("dayOfWeek") or d.get("dow") or 1)
            except (ValueError, TypeError):
                dow = 1

            status = str(d.get("dayStatus") or d.get("status") or "WD").upper()
            is_holiday = status == "HD" or bool(d.get("isHoliday", False))
            desc = str(d.get("desc") or d.get("description") or "").strip()

            days.append(
                CalendarDay(
                    date=date_str,
                    day_of_week=dow,
                    status=status,
                    is_holiday=is_holiday,
                    description=desc,
                )
            )

        days.sort(key=lambda x: x.date)
        self._cache_set(cache_key, days)
        return days

    async def get_periods(self) -> list[SchoolPeriod]:
        """Fetches school terms and evaluation periods from /students/{id}/periods."""
        cache_key = "periods"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        resp = await self._request("GET", "/students/{student_id}/periods")
        data = resp.json()
        if isinstance(data, list):
            raw_periods = data
        elif isinstance(data, dict):
            raw_periods = data.get("periods") or []
        else:
            raw_periods = []

        periods: list[SchoolPeriod] = []
        for p in raw_periods:
            if not isinstance(p, dict):
                continue
            p_code = str(p.get("periodCode") or p.get("code") or "").strip()
            try:
                p_pos = int(p.get("periodPos") or p.get("pos") or 1)
            except (ValueError, TypeError):
                p_pos = 1
            desc = str(p.get("periodDesc") or p.get("description") or "").strip()
            is_final = bool(p.get("isFinal", False))
            d_start = str(p.get("dateStart") or "")[:10]
            d_end = str(p.get("dateEnd") or "")[:10]

            periods.append(
                SchoolPeriod(
                    period_code=p_code,
                    period_pos=p_pos,
                    description=desc,
                    is_final=is_final,
                    date_start=d_start,
                    date_end=d_end,
                )
            )

        periods.sort(key=lambda x: x.period_pos)
        self._cache_set(cache_key, periods)
        return periods

    async def get_notes(
        self, period_filter: str | None = None
    ) -> list[DisciplinaryNote]:
        """Fetches disciplinary notes, teacher annotations, and sanctions from /students/{id}/notes/all.

        Iterates through categories:
        - NTTE: Annotazione del docente
        - NTCL: Nota di classe
        - NTWN: Richiamo formale / avvertimento
        - NTST: Sanzione disciplinare

        If note body (evtText) is empty or truncated, calls POST /students/{id}/notes/{kind}/read/{evtId}
        to fetch full text.
        """
        resp = await self._request("GET", "/students/{student_id}/notes/all")
        data = resp.json()

        kind_descriptions = {
            "NTTE": "Nota del docente",
            "NTCL": "Nota di classe",
            "NTWN": "Richiamo",
            "NTST": "Sanzione disciplinare",
        }

        grouped_items: list[tuple[str, dict[str, Any]]] = []
        if isinstance(data, dict):
            for kind in ("NTTE", "NTCL", "NTWN", "NTST"):
                for it in (data.get(kind) or []):
                    if isinstance(it, dict):
                        grouped_items.append((kind, it))
            if not grouped_items and "notes" in data and isinstance(data["notes"], list):
                for it in data["notes"]:
                    if isinstance(it, dict):
                        k = str(it.get("evtCode") or it.get("kind") or "NTTE").upper()
                        grouped_items.append((k, it))
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, dict):
                    k = str(it.get("evtCode") or it.get("kind") or "NTTE").upper()
                    grouped_items.append((k, it))

        notes: list[DisciplinaryNote] = []
        for kind, it in grouped_items:
            evt_id = str(it.get("evtId") or it.get("id") or "")
            evt_date = str(it.get("evtDate") or it.get("date") or "")[:10]
            teacher = str(it.get("authorName") or it.get("teacher") or "").strip().title()
            text = str(it.get("evtText") or it.get("text") or it.get("notes") or "").strip()
            is_read = bool(it.get("readStatus", False))
            period = str(it.get("periodCode") or it.get("period") or "").strip()

            if period_filter and period and period_filter.lower() not in period.lower():
                continue

            if not text and evt_id:
                try:
                    read_resp = await self._request(
                        "POST",
                        f"/students/{{student_id}}/notes/{kind}/read/{evt_id}",
                    )
                    r_json = read_resp.json()
                    note_body = (
                        r_json.get("item")
                        or r_json.get("note")
                        or (r_json if isinstance(r_json, dict) else {})
                    )
                    text = str(
                        note_body.get("evtText") or note_body.get("text") or ""
                    ).strip()
                    is_read = True
                except Exception as exc:
                    logger.warning(
                        "Impossibile recuperare il corpo della nota %s: %s", evt_id, exc
                    )

            notes.append(
                DisciplinaryNote(
                    id=evt_id,
                    kind=kind,
                    kind_description=kind_descriptions.get(kind, "Nota disciplinare"),
                    date=evt_date,
                    teacher=teacher,
                    text=text,
                    is_read=is_read,
                )
            )

        notes.sort(key=lambda x: x.date, reverse=True)
        return notes

    async def get_documents(self) -> list[SchoolDocument]:
        """Fetches official school documents and report cards from POST /students/{id}/documents."""
        cache_key = "documents"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        resp = await self._request("POST", "/students/{student_id}/documents")
        data = resp.json()
        raw_docs: list[dict[str, Any]] = []
        if isinstance(data, list):
            raw_docs.extend(data)
        elif isinstance(data, dict):
            raw_docs.extend(data.get("documents") or [])
            raw_docs.extend(data.get("schoolReports") or [])

        docs: list[SchoolDocument] = []
        for d in raw_docs:
            if not isinstance(d, dict):
                continue
            d_hash = str(d.get("hash") or d.get("id") or d.get("docHash") or "").strip()
            title = str(
                d.get("desc") or d.get("title") or d.get("description") or "Documento"
            ).strip()
            d_type = str(d.get("docType") or d.get("type") or "").strip()
            pub_date = str(d.get("pubDT") or d.get("publishDate") or d.get("date") or "")[:10]

            if d_hash:
                docs.append(
                    SchoolDocument(
                        hash=d_hash,
                        title=title,
                        document_type=d_type,
                        publish_date=pub_date,
                    )
                )

        docs.sort(key=lambda x: x.publish_date, reverse=True)
        self._cache_set(cache_key, docs)
        return docs

    async def download_document(
        self,
        doc_hash: str,
        *,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> bytes:
        """Downloads an official school document (e.g. report card) with byte streaming."""
        if not self._token or not self._student_id:
            await self.login()

        url = f"{BASE_URL}/students/{self._student_id}/documents/read/{doc_hash}"
        headers = dict(DEFAULT_HEADERS)
        headers.pop("Content-Type", None)
        headers["Z-Auth-Token"] = self._token

        client = (
            self._external_client
            if self._external_client is not None
            else self._http()
        )
        async with client.stream("POST", url, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise ClasseVivaError("Redirect documento senza header Location.")
                target = urljoin(url, location)
                parsed = urlparse(target)
                if parsed.scheme != "https" or not self._is_allowed_attachment_host(
                    parsed.hostname or ""
                ):
                    raise ClasseVivaError("Redirect documento verso host non consentito.")
                async with client.stream("GET", target, headers={}) as cdn_response:
                    cdn_response.raise_for_status()
                    return await self._read_limited(cdn_response, max_bytes)

            response.raise_for_status()
            return await self._read_limited(response, max_bytes)

    async def get_schoolbooks(
        self, subject_filter: str | None = None
    ) -> list[SchoolBook]:
        """Fetches adopted school textbooks from /students/{id}/schoolbooks."""
        cache_key = "schoolbooks"
        cached = self._cache_get(cache_key)
        if cached is None:
            cached = await self._fetch_json(
                cache_key, "/students/{student_id}/schoolbooks", "schoolbooks"
            )

        books: list[SchoolBook] = []
        for b in cached:
            if not isinstance(b, dict):
                continue
            subj = str(
                b.get("subject") or b.get("materia") or b.get("subjectDesc") or ""
            ).strip().title()
            if subject_filter and subject_filter.lower() not in subj.lower():
                continue

            isbn = str(b.get("isbn") or b.get("codiceIsbn") or "").strip()
            title = str(b.get("title") or b.get("titolo") or "").strip()
            authors = str(
                b.get("authors") or b.get("author") or b.get("autori") or b.get("authorName") or ""
            ).strip().title()
            publisher = str(b.get("publisher") or b.get("editore") or "").strip()
            volume = str(b.get("volume") or b.get("vol") or "").strip()

            raw_price = b.get("price") or b.get("prezzo")
            price: float | None = None
            if raw_price is not None:
                try:
                    price = float(str(raw_price).replace(",", "."))
                except (ValueError, TypeError):
                    price = None

            is_rec = bool(b.get("isRecommended") or b.get("consigliato", False))
            to_buy_val = (
                b.get("toBuy") if b.get("toBuy") is not None else b.get("daAcquistare")
            )
            to_buy = bool(to_buy_val) if to_buy_val is not None else (not is_rec)

            books.append(
                SchoolBook(
                    isbn=isbn,
                    title=title,
                    authors=authors,
                    publisher=publisher,
                    subject=subj,
                    volume=volume,
                    price=price,
                    to_buy=to_buy,
                    is_recommended=is_rec,
                )
            )

        books.sort(key=lambda x: (x.subject, x.title))
        return books

