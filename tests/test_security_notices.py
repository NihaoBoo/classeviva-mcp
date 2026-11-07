from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import classeviva_mcp.client as client_module
from classeviva_mcp.client import (
    MAX_ATTACHMENT_BYTES,
    ClasseVivaError,
    SpaggiariClient,
)
from classeviva_mcp.credentials import CredentialStore

NOTICE_PAYLOAD = {
    "items": [
        {
            "pubId": 245,
            "evtCode": "CF",
            "cntTitle": "Circolare di prova",
            "cntCategory": "Didattica",
            "pubDT": "2026-11-06T11:00:00",
            "readStatus": False,
            "attachments": [{"attachNum": 1, "fileName": "allegato.pdf"}],
        }
    ]
}

MULTI_ATTACHMENT_PAYLOAD = {
    "items": [
        {
            "pubId": 245,
            "evtCode": "CF",
            "cntTitle": "Circolare con tre allegati",
            "pubDT": "2026-11-06T11:00:00",
            "readStatus": True,
            "attachments": [
                {"attachNum": 1, "fileName": "a.pdf"},
                {"attachNum": 2, "fileName": "b.pdf"},
                {"attachNum": 3, "fileName": "c.pdf"},
            ],
        }
    ]
}


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)


def _client(tmp_path: Path, handler: Any) -> tuple[SpaggiariClient, httpx.AsyncClient]:
    store = _store(tmp_path)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpaggiariClient(store=store, http_client=http_client)
    client._token = "fake-token"
    client._student_id = "1234567"
    return client, http_client


@pytest.mark.asyncio
async def test_unread_notice_is_not_marked_when_disallowed(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=NOTICE_PAYLOAD)
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF", pub_id="245", allow_mark_as_read=False
        )

    assert result["skipped"] is True
    assert result["marked_as_read"] is False
    assert result["was_unread"] is True
    assert result["attachments"] == []
    assert not any("/noticeboard/read/" in call for call in calls)
    assert not any("/noticeboard/attach/" in call for call in calls)


@pytest.mark.asyncio
async def test_unread_notice_can_be_marked_and_downloaded(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=NOTICE_PAYLOAD)
        if "/noticeboard/read/" in request.url.path:
            return httpx.Response(200, json={"item": {"cntText": "Testo integrale"}})
        if "/noticeboard/attach/" in request.url.path:
            return httpx.Response(
                302, headers={"location": "https://cdndoc.spaggiari.eu/signed/allegato.pdf"}
            )
        if request.url.host == "cdndoc.spaggiari.eu":
            return httpx.Response(200, content=b"%PDF-1.4 contenuto")
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF", pub_id="245", allow_mark_as_read=True
        )

    assert result["skipped"] is False
    assert result["marked_as_read"] is True
    assert result["text_body"] == "Testo integrale"
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["bytes"].startswith(b"%PDF")
    assert any("/noticeboard/read/" in call for call in calls)


@pytest.mark.asyncio
async def test_attachment_redirect_to_foreign_host_is_blocked(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/noticeboard/attach/" in request.url.path:
            return httpx.Response(302, headers={"location": "https://evil.example/steal.pdf"})
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="host non consentito"):
            await client._download_attachment("CF", "245", 1)


@pytest.mark.asyncio
async def test_attachment_size_cap_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_ATTACHMENT_BYTES", 8)

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/noticeboard/attach/" in request.url.path:
            return httpx.Response(
                302, headers={"location": "https://cdndoc.spaggiari.eu/signed/allegato.pdf"}
            )
        if request.url.host == "cdndoc.spaggiari.eu":
            return httpx.Response(200, content=b"x" * 32)
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(ClasseVivaError, match="troppo grande"):
            await client._download_attachment("CF", "245", 1)
    assert MAX_ATTACHMENT_BYTES == 20 * 1024 * 1024


@pytest.mark.asyncio
async def test_unread_notice_defaults_to_no_mark_as_read(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=NOTICE_PAYLOAD)
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(event_code="CF", pub_id="245")

    assert result["skipped"] is True
    assert result["marked_as_read"] is False
    assert not any("/noticeboard/read/" in call for call in calls)
    assert not any("/noticeboard/attach/" in call for call in calls)


@pytest.mark.asyncio
async def test_attachment_index_downloads_only_selected_attachment(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=MULTI_ATTACHMENT_PAYLOAD)
        if "/noticeboard/read/" in request.url.path:
            return httpx.Response(200, json={"item": {"cntText": "Testo"}})
        if "/noticeboard/attach/" in request.url.path:
            return httpx.Response(200, content=b"%PDF-1.4 allegato")
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF", pub_id="245", attachment_index=1
        )

    attach_calls = [c for c in calls if "/noticeboard/attach/" in c]
    assert len(attach_calls) == 1
    assert attach_calls[0].endswith("/2")
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["attach_num"] == 2


@pytest.mark.asyncio
async def test_total_attachment_budget_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_NOTICE_TOTAL_BYTES", 10)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=MULTI_ATTACHMENT_PAYLOAD)
        if "/noticeboard/read/" in request.url.path:
            return httpx.Response(200, json={"item": {"cntText": "Testo"}})
        if "/noticeboard/attach/" in request.url.path:
            return httpx.Response(200, content=b"x" * 8)
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(event_code="CF", pub_id="245")

    assert len(result["attachments"]) == 1
    assert len(result["attachments"][0]["bytes"]) == 8


@pytest.mark.asyncio
async def test_list_notices_never_triggers_read_receipt(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/noticeboard"):
            return httpx.Response(200, json=NOTICE_PAYLOAD)
        raise AssertionError(f"Chiamata inattesa: {request.method} {request.url}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        notices = await client.list_notices()

    assert len(notices) == 1
    assert notices[0].is_read is False
    assert calls == ["GET /rest/v1/students/1234567/noticeboard"]
