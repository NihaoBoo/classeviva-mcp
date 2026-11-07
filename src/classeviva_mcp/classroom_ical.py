from __future__ import annotations

import hashlib
from datetime import date, datetime
import httpx
from icalendar import Calendar

from classeviva_mcp.models import AgendaItem, ROME_TZ

CLASSROOM_API_BASE = "https://classroom.googleapis.com/v1"


async def fetch_classroom_tasks(
    ical_url: str = "",
    google_access_token: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    """Dual-mode Google Classroom fetcher:
    1. If `google_access_token` (OAuth2) is provided and allowed by school domain, queries Classroom API.
    2. If `ical_url` (.ics secret link) is provided, fetches and parses VEVENT deadlines (bypasses domain admin blocks).
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
        except Exception:
            pass

    if ical_url.strip():
        try:
            ical_items = await fetch_classroom_ical_tasks(
                ical_url.strip(),
                start_date=start_date,
                end_date=end_date,
            )
            results.extend(ical_items)
        except Exception:
            pass

    return results


async def fetch_classroom_oauth_tasks(
    access_token: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    headers = {"Authorization": f"Bearer {access_token}"}
    items: list[AgendaItem] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
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
                        title=work.get("title", "Compito Classroom"),
                        description=work.get("description", ""),
                        date=date_str,
                        url=work.get("alternateLink"),
                    )
                )
    items.sort(key=lambda x: x.date)
    return items


async def fetch_classroom_ical_tasks(
    ical_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[AgendaItem]:
    """Downloads and parses a Google Classroom secret iCal (.ics) feed into AgendaItem objects."""
    clean_url = (ical_url or "").strip()
    if not clean_url:
        return []
    if clean_url.startswith("webcal://"):
        clean_url = "https://" + clean_url[len("webcal://") :]

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(clean_url)
        resp.raise_for_status()
        raw_bytes = resp.content

    return parse_classroom_ics(raw_bytes, start_date=start_date, end_date=end_date)


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
        summary = str(comp.get("SUMMARY", "Compito Classroom")).strip()
        desc = str(comp.get("DESCRIPTION", "")).strip()
        url = str(comp.get("URL", "")).strip() or None

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
