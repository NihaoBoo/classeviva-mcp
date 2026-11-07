from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import httpx
from icalendar import Calendar

from classeviva_mcp.models import ROME_TZ, AgendaItem

logger = logging.getLogger(__name__)

CLASSROOM_API_BASE = "https://classroom.googleapis.com/v1"
ALLOWED_ICAL_HOSTS = frozenset({"calendar.google.com", "classroom.google.com"})
MAX_ICAL_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_REDIRECTS = 3
MAX_FIELD_CHARS = 4000


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


async def fetch_classroom_tasks(
    ical_url: str = "",
    google_access_token: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    """Dual-mode Google Classroom fetcher:
    1. If `google_access_token` (OAuth2) is provided, queries the Classroom API.
    2. If `ical_url` (.ics secret link) is provided, fetches and parses VEVENT deadlines.
    """
    results: list[AgendaItem] = []
    if google_access_token.strip():
        try:
            oauth_items = await fetch_classroom_oauth_tasks(
                google_access_token.strip(),
                start_date=start_date,
                end_date=end_date,
            )
            results.extend(oauth_items)
        except Exception as exc:  # noqa: BLE001 - degraded mode, never leak token/URL
            logger.warning("Classroom OAuth non disponibile: %s", type(exc).__name__)

    if ical_url.strip():
        try:
            ical_items = await fetch_classroom_ical_tasks(
                ical_url.strip(),
                start_date=start_date,
                end_date=end_date,
            )
            results.extend(ical_items)
        except Exception as exc:  # noqa: BLE001 - degraded mode, never leak token/URL
            logger.warning("Classroom iCal non disponibile: %s", type(exc).__name__)

    return results


async def fetch_classroom_oauth_tasks(
    access_token: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    headers = {"Authorization": f"Bearer {access_token}"}
    items: list[AgendaItem] = []

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, trust_env=False) as client:
        c_resp = await client.get(
            f"{CLASSROOM_API_BASE}/courses",
            headers=headers,
            params={"courseStates": "ACTIVE"},
        )
        c_resp.raise_for_status()
        for course in c_resp.json().get("courses") or []:
            course_id = course["id"]
            course_name = course.get("name", "Classroom")
            cw_resp = await client.get(
                f"{CLASSROOM_API_BASE}/courses/{course_id}/courseWork",
                headers=headers,
                params={"orderBy": "dueDate desc", "pageSize": 15},
            )
            if cw_resp.status_code != 200:
                continue
            for work in cw_resp.json().get("courseWork") or []:
                due = work.get("dueDate")
                if not due:
                    continue
                date_str = f"{int(due['year']):04d}-{int(due['month']):02d}-{int(due['day']):02d}"
                if start_date and date_str < start_date:
                    continue
                if end_date and date_str > end_date:
                    continue
                items.append(
                    AgendaItem(
                        id=f"gcal_oauth:{work['id']}",
                        source="classroom_oauth",
                        kind="assignment",
                        subject=course_name,
                        title=str(work.get("title", "Compito Classroom"))[:500],
                        description=str(work.get("description", ""))[:MAX_FIELD_CHARS],
                        date=date_str,
                        url=str(work.get("alternateLink") or "")[:2000] or None,
                    )
                )
    items.sort(key=lambda x: x.date)
    return items


async def fetch_classroom_ical_tasks(
    ical_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[AgendaItem]:
    """Downloads and parses a Google Classroom secret iCal (.ics) feed into AgendaItem objects."""
    clean_url = validate_ical_url(ical_url)
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=False,
        trust_env=False,
    )
    try:
        for _ in range(MAX_REDIRECTS + 1):
            response = await client.get(clean_url, headers={"Accept-Encoding": "identity"})
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Redirect iCal senza header Location.")
                clean_url = validate_ical_url(urljoin(clean_url, location))
                continue
            response.raise_for_status()
            raw_bytes = await _read_limited(response, MAX_ICAL_BYTES)
            return parse_classroom_ics(raw_bytes, start_date=start_date, end_date=end_date)
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
