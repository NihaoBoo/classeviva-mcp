from __future__ import annotations

import socket

import httpx
import pytest

from classeviva_mcp.classroom_ical import (
    MAX_ICAL_BYTES,
    fetch_classroom_ical_tasks,
    validate_ical_url,
)

PUBLIC_IP = "142.250.184.78"


def _public_dns(host: str, port: int, **kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))]


def test_validate_ical_url_accepts_google_and_webcal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    url = "https://calendar.google.com/calendar/ical/test/basic.ics"
    assert validate_ical_url(url) == url
    assert validate_ical_url("webcal://calendar.google.com/calendar/ical/test/basic.ics").startswith(
        "https://"
    )
    assert validate_ical_url("https://calendar.google.com./calendar/ical/test/basic.ics")


@pytest.mark.parametrize(
    "url",
    [
        "http://calendar.google.com/calendar/ical/test/basic.ics",
        "ftp://calendar.google.com/calendar/ical/test/basic.ics",
        "https://localhost/calendar/ical/test/basic.ics",
        "https://127.0.0.1/calendar/ical/test/basic.ics",
        "https://169.254.169.254/latest/meta-data/",
        "https://calendar.google.com@evil.example/calendar/ical/test/basic.ics",
        "https://evil.example/calendar/ical/test/basic.ics",
        "https://calendar.google.com:8443/calendar/ical/test/basic.ics",
    ],
)
def test_validate_ical_url_rejects_unsafe_urls(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    with pytest.raises(ValueError):
        validate_ical_url(url)


@pytest.mark.parametrize("resolved_ip", ["10.0.0.1", "192.168.1.10", "127.0.0.1", "::1", "169.254.169.254"])
def test_validate_ical_url_rejects_private_dns(
    monkeypatch: pytest.MonkeyPatch, resolved_ip: str
) -> None:
    def _private_dns(host: str, port: int, **kwargs: object) -> list[tuple]:
        family = socket.AF_INET6 if ":" in resolved_ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (resolved_ip, 443, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _private_dns)
    with pytest.raises(ValueError):
        validate_ical_url("https://calendar.google.com/calendar/ical/test/basic.ics")


@pytest.mark.asyncio
async def test_ical_redirect_to_private_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://10.0.0.1/secret.ics"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        with pytest.raises(ValueError):
            await fetch_classroom_ical_tasks(
                "https://calendar.google.com/calendar/ical/test/basic.ics",
                http_client=client,
            )


@pytest.mark.asyncio
async def test_ical_oversize_response_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_ICAL_BYTES + 1))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        with pytest.raises(ValueError):
            await fetch_classroom_ical_tasks(
                "https://calendar.google.com/calendar/ical/test/basic.ics",
                http_client=client,
            )


@pytest.mark.asyncio
async def test_ical_valid_feed_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:abc\r\n"
        b"SUMMARY:[Informatica] Compito di prova\r\n"
        b"DTEND:20261120T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ics, headers={"content-type": "text/calendar"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        items = await fetch_classroom_ical_tasks(
            "https://calendar.google.com/calendar/ical/test/basic.ics",
            http_client=client,
        )
    assert len(items) == 1
    assert items[0].subject == "Informatica"
    assert items[0].date == "2026-11-20"
