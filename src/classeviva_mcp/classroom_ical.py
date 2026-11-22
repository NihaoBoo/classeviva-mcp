from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from icalendar import Calendar

from classeviva_mcp.models import ROME_TZ, AgendaItem
from classeviva_mcp.network import build_async_client

logger = logging.getLogger(__name__)

CLASSROOM_API_BASE = "https://classroom.googleapis.com/v1"
ALLOWED_ICAL_HOSTS = frozenset({"calendar.google.com", "classroom.google.com"})
MAX_ICAL_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_REDIRECTS = 3
MAX_FIELD_CHARS = 4000
MAX_API_PAGES = 100
MAX_API_SECONDS = 25


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(ip.is_global and not ip.is_multicast)


def validate_ical_url(url: str) -> str:
    """Validate an iCal URL against a strict Google-only allowlist.

    Rejects non-HTTPS schemes, credentials in the URL, non-standard ports,
    non-allowlisted hosts, and hosts resolving to private/reserved addresses.
    """
    clean = (url or "").strip()
    if not clean:
        raise ValueError("URL iCal vuoto.")
    if clean.startswith("webcal://"):
        clean = "https://" + clean[len("webcal://") :]

    parsed = urlparse(clean)
    if parsed.scheme != "https":
        raise ValueError("Schema URL non consentito: è ammesso solo https.")
    if parsed.username or parsed.password:
        raise ValueError("URL iCal non valido: credenziali nell'URL non consentite.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in ALLOWED_ICAL_HOSTS:
        raise ValueError(
            "Host iCal non consentito. Sono ammessi solo: "
            + ", ".join(sorted(ALLOWED_ICAL_HOSTS))
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta URL iCal non valida.") from exc
    if port not in (None, 443):
        raise ValueError("Porta URL iCal non consentita: è ammessa solo la 443.")

    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Impossibile risolvere il dominio del feed iCal.") from exc
    for info in infos:
        ip = str(info[4][0])
        if not _is_public_ip(ip):
            raise ValueError("Il feed iCal risolve verso un indirizzo privato o riservato.")

    return clean


async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > max_bytes:
        raise ValueError(f"Risposta troppo grande (limite {max_bytes // (1024 * 1024)} MB).")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Risposta troppo grande (limite {max_bytes // (1024 * 1024)} MB).")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class ClassroomFetchResult:
    """Partial results from Google Classroom plus the reasons some source failed.

    Errors are returned instead of being swallowed: `get_homework` must be able
    to tell the agent that Classroom is unavailable, otherwise an empty list
    looks like "no homework" (fail-open).
    """

    items: list[AgendaItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _ical_warning(exc: Exception) -> str:
    """Builds a user-facing warning without leaking the secret feed URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (404, 410):
            return (
                "Google Classroom (iCal): link non più valido "
                f"(HTTP {status}). Rigenera l'indirizzo segreto e rilancia "
                "`classeviva-mcp set-classroom-url`."
            )
        return f"Google Classroom (iCal): feed non raggiungibile (HTTP {status})."
    return f"Google Classroom (iCal): feed non valido ({type(exc).__name__})."


async def fetch_classroom_tasks(
    ical_url: str = "",
    google_access_token: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> ClassroomFetchResult:
    """Dual-mode Google Classroom fetcher:
    1. If `google_access_token` (OAuth2) is provided, queries the Classroom API.
    2. If `ical_url` (.ics secret link) is provided, fetches and parses VEVENT deadlines.

    Never raises for a single failing source: returns the items that worked and
    a `warnings` list describing what failed (without the URL or token).
    """
    result = ClassroomFetchResult()
    oauth_configured = bool(google_access_token.strip())
    if oauth_configured:
        try:
            result = await fetch_classroom_oauth_tasks(
                google_access_token.strip(),
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001 - degraded mode, never leak token/URL
            logger.warning("Classroom OAuth non disponibile: %s", type(exc).__name__)
            result.warnings.append(
                f"Google Classroom (OAuth): accesso non riuscito ({type(exc).__name__})."
            )

    if ical_url.strip() and (not oauth_configured or result.warnings):
        try:
            ical_items = await fetch_classroom_ical_tasks(
                ical_url.strip(),
                start_date=start_date,
                end_date=end_date,
            )
            # A degraded OAuth query may already contain deadlines from the feed.
            known = {(it.date, it.title.casefold()) for it in result.items}
            result.items.extend(it for it in ical_items if (it.date, it.title.casefold()) not in known)
        except Exception as exc:  # noqa: BLE001 - degraded mode, never leak token/URL
            logger.warning("Classroom iCal non disponibile: %s", type(exc).__name__)
            result.warnings.append(_ical_warning(exc))

    return result


class _ClassroomAPI:
    """Bounded pagination shared by deadlines and materials."""

    def __init__(self, client: httpx.AsyncClient, access_token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.remaining = MAX_API_PAGES
        self.incomplete = False
        self.auth_failed = False

    async def pages(
        self, path: str, key: str, **query: str
    ) -> AsyncIterator[dict[str, Any]]:
        seen: set[str] = set()
        token = ""
        while self.remaining and not self.auth_failed:
            self.remaining -= 1
            params = {**query, "pageSize": "50"}
            if token:
                params["pageToken"] = token
            try:
                async with self.client.stream(
                    "GET", f"{CLASSROOM_API_BASE}/{path}", headers=self.headers, params=params,
                ) as response:
                    if response.status_code == 401:
                        self.auth_failed = True
                    response.raise_for_status()
                    data = json.loads(await _read_limited(response, MAX_ICAL_BYTES))
                if not isinstance(data, dict):
                    raise ValueError
                page = data.get(key, [])
                if not isinstance(page, list):
                    raise ValueError
                for item in page:
                    if isinstance(item, dict):
                        yield item
                    else:
                        self.incomplete = True
                token = data.get("nextPageToken") or ""
                if not token:
                    return
                if not isinstance(token, str) or token in seen or len(token) > 4096:
                    raise ValueError
                seen.add(token)
            except (httpx.HTTPError, ValueError):
                break
        self.incomplete = True

    async def entries(self, *, materials: bool = False) -> AsyncIterator[tuple[str, str, str, dict[str, Any]]]:
        async for course in self.pages("courses", "courses", courseStates="ACTIVE"):
            course_id = str(course.get("id", ""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", course_id):
                self.incomplete = True
                continue
            course_name = str(course.get("name") or "Classroom")[:200]
            async for entry in self.pages(
                f"courses/{course_id}/courseWork", "courseWork",
                **({} if materials else {"orderBy": "dueDate desc"}),
            ):
                yield course_id, course_name, "work", entry
            if materials:
                async for entry in self.pages(f"courses/{course_id}/courseWorkMaterials", "courseWorkMaterial"):
                    yield course_id, course_name, "material", entry
            if self.auth_failed or not self.remaining:
                self.incomplete = True
                return

    def warnings(self) -> list[str]:
        if self.auth_failed:
            return ["Google Classroom: verifica incompleta; riconnetti l'account con `classroom login`."]
        if self.incomplete:
            return ["Google Classroom: verifica incompleta; alcune pagine o corsi non sono stati verificati."]
        return []


def _deadline(due: dict[str, Any], clock: dict[str, Any] | None) -> tuple[str, str | None]:
    day = date(int(due["year"]), int(due["month"]), int(due["day"]))
    if clock is None:
        return day.isoformat(), None
    local = datetime(
        day.year, day.month, day.day, int(clock.get("hours", 0)), int(clock.get("minutes", 0)),
        int(clock.get("seconds", 0)), tzinfo=UTC,
    ).astimezone(ROME_TZ)
    return local.date().isoformat(), local.strftime("%H:%M")


async def fetch_classroom_oauth_tasks(
    access_token: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ClassroomFetchResult:
    items: dict[str, AgendaItem] = {}
    async with build_async_client(timeout=8.0, follow_redirects=False) as client:
        api = _ClassroomAPI(client, access_token)
        try:
            async with asyncio.timeout(MAX_API_SECONDS):
                async for course_id, course_name, _, work in api.entries():
                    due = work.get("dueDate")
                    if not due:
                        continue
                    try:
                        date_str, time = _deadline(due, work.get("dueTime"))
                        item_id = f"gcal_oauth:{course_id}:{work['id']}"
                    except (ValueError, TypeError, KeyError, AttributeError):
                        api.incomplete = True
                        continue
                    if (start_date and date_str < start_date) or (end_date and date_str > end_date):
                        continue
                    items[item_id] = AgendaItem(
                        id=item_id, source="classroom_oauth", kind="assignment", subject=course_name,
                        title=str(work.get("title") or "Compito Classroom")[:500],
                        description=str(work.get("description") or "")[:MAX_FIELD_CHARS],
                        date=date_str, time=time, url=_google_link(work.get("alternateLink")),
                    )
        except TimeoutError:
            api.incomplete = True
    return ClassroomFetchResult(sorted(items.values(), key=lambda item: (item.date, item.time or "")), api.warnings())


def _google_link(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme == "https" and parsed.hostname in {"classroom.google.com", "drive.google.com", "docs.google.com"}
            and parsed.username is None and parsed.password is None and parsed.port in (None, 443)
        ):
            return value
    except ValueError:
        pass
    return None


async def fetch_classroom_materials(access_token: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Metadata only. Never dereference attachment or external URLs."""
    items: dict[str, dict[str, Any]] = {}
    async with build_async_client(timeout=8.0, follow_redirects=False) as client:
        api = _ClassroomAPI(client, access_token)
        try:
            async with asyncio.timeout(MAX_API_SECONDS):
                async for course_id, subject, kind, entry in api.entries(materials=True):
                    if not entry.get("id"):
                        api.incomplete = True
                        continue
                    post_link = _google_link(entry.get("alternateLink"))
                    post_title = str(entry.get("title") or "Materiale Classroom")[:240]
                    date_str = str(entry.get("creationTime") or "")[:10]
                    materials = entry.get("materials") or ([{}] if kind == "material" else [])
                    if not isinstance(materials, list):
                        api.incomplete = True
                        continue
                    for index, material in enumerate(materials):
                        if not isinstance(material, dict):
                            api.incomplete = True
                            continue
                        drive_wrapper = material.get("driveFile")
                        drive = drive_wrapper.get("driveFile") if isinstance(drive_wrapper, dict) else None
                        target = next(
                            (part for part in (drive, material.get("link"), material.get("youtubeVideo"))
                             if isinstance(part, dict)),
                            {},
                        )
                        title = str(target.get("title") or post_title)[:240]
                        link = _google_link(target.get("alternateLink")) or post_link
                        item_id = f"classroom:{course_id}:{kind}:{entry['id']}:{index}"
                        items[item_id] = {
                            "id": item_id, "title": title, "source": "classroom", "date": date_str, "subject": subject,
                            **({"url": link} if link else {}),
                        }
        except TimeoutError:
            api.incomplete = True
    return list(items.values()), api.warnings()


async def fetch_classroom_ical_tasks(
    ical_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[AgendaItem]:
    """Downloads and parses a Google Classroom secret iCal (.ics) feed into AgendaItem objects."""
    clean_url = validate_ical_url(ical_url)
    own_client = http_client is None
    client = http_client or build_async_client(
        timeout=15.0,
        follow_redirects=False,
    )
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream(
                "GET", clean_url, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("Redirect iCal senza header Location.")
                    clean_url = validate_ical_url(urljoin(clean_url, location))
                    continue
                response.raise_for_status()
                raw_bytes = await _read_limited(response, MAX_ICAL_BYTES)
                return parse_classroom_ics(
                    raw_bytes, start_date=start_date, end_date=end_date
                )
        raise ValueError("Troppi redirect nel feed iCal.")
    finally:
        if own_client:
            await client.aclose()


def parse_classroom_ics(
    raw_bytes: bytes,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    cal = Calendar.from_ical(raw_bytes)
    items: list[AgendaItem] = []

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        uid = str(comp.get("UID", ""))
        summary = str(comp.get("SUMMARY", "Compito Classroom")).strip()[:500]
        desc = str(comp.get("DESCRIPTION", "")).strip()[:MAX_FIELD_CHARS]
        url = str(comp.get("URL", "")).strip()[:2000] or None

        dt_prop = comp.get("DTEND") or comp.get("DTSTART")
        if not dt_prop:
            continue
        dt_val = dt_prop.dt
        if isinstance(dt_val, datetime):
            if dt_val.tzinfo:
                dt_val = dt_val.astimezone(ROME_TZ)
            date_str = dt_val.strftime("%Y-%m-%d")
        elif isinstance(dt_val, date):
            date_str = dt_val.strftime("%Y-%m-%d")
        else:
            date_str = str(dt_val)[:10]

        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue

        subject = "Classroom"
        title = summary
        if summary.startswith("[") and "]" in summary:
            subj_part, title_part = summary.split("]", 1)
            subject = subj_part.lstrip("[").strip()
            title = title_part.strip()

        stable_id = uid or hashlib.sha256(f"{summary}:{date_str}".encode()).hexdigest()[:14]
        items.append(
            AgendaItem(
                id=f"ical:{stable_id}",
                source="classroom_ical",
                kind="assignment",
                subject=subject,
                title=title,
                description=desc,
                date=date_str,
                url=url,
            )
        )

    items.sort(key=lambda x: x.date)
    return items
