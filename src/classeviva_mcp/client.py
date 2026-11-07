from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.models import (
    AbsenceItem,
    AgendaItem,
    GradeItem,
    LessonItem,
    NoticeItem,
    StudentProfile,
    SubjectTeacher,
)

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

logger = logging.getLogger(__name__)


class ClasseVivaError(Exception):
    """Base error for ClasseViva client operations."""


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
            self._owned_client = httpx.AsyncClient(
                timeout=25.0,
                follow_redirects=False,
                trust_env=False,
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
        return await self._http().post(
            f"{BASE_URL}/auth/login",
            headers=DEFAULT_HEADERS,
            json=payload,
        )

    async def login(self) -> StudentProfile:
        """Logs in using stored credentials (email, S-code, or G-code).
        Automatically resolves multi-profile `choices` if returned by Spaggiari.

        When ``credentials`` were injected at construction time they are used
        instead of the persisted ones, and the session is not written to disk
        unless ``persist_session`` is True. This lets the CLI validate a
        password against Spaggiari *before* storing it.
        """
        creds = self._credentials or self.store.load()
        payload: dict[str, Any] = {
            "ident": creds.preferred_profile or None,
            "pass": creds.password,
            "uid": creds.username,
        }

        resp = await self._raw_post_login(payload)
        data = resp.json() if resp.content else {}

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
            data = resp.json() if resp.content else {}

        if resp.status_code in (401, 403, 422) or not data.get("token"):
            err_msg = str(data.get("error") or resp.text or "Credenziali rifiutate")
            if "otp" in err_msg.lower() or "2fa" in err_msg.lower():
                raise ClasseVivaError("Spaggiari richiede verifica OTP/2FA per questo account.")
            raise ClasseVivaError(f"Login ClasseViva fallito (HTTP {resp.status_code}): {err_msg}")

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
                resp = await self._http().request(method, url, headers=headers, **kwargs)

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

        if self._external_client is not None:
            return await self._download_attachment_with_client(
                self._external_client, url, headers, max_bytes
            )
        return await self._download_attachment_with_client(
            self._http(), url, headers, max_bytes
        )

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
            subject = (ev.get("subjectDesc") or ev.get("authorName") or "Generale").strip().title()
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
                    title=notes[:100] if notes else f"{kind.title()} {subject}",
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
        items = resp.json().get(field) or []
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
                logger.warning("Allegato non scaricato (%s): %s", num, type(exc).__name__)
                continue

        primary_bytes = downloaded[0]["bytes"] if downloaded else b""
        return {
            "text_body": text_body,
            "notice_title": notice_title,
            "pdf_bytes": primary_bytes,
            "attachments": downloaded,
            "marked_as_read": marked_as_read,
            "was_unread": was_unread,
            "read_state_known": True,
            "skipped": False,
        }
