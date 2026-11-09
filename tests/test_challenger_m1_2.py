from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from classeviva_mcp.client import SpaggiariClient
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import SchoolBook


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)


def _client(tmp_path: Path, handler: Any) -> tuple[SpaggiariClient, httpx.AsyncClient]:
    store = _store(tmp_path)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpaggiariClient(store=store, http_client=http_client)
    client._token = "test-token"
    client._student_id = "1234567"
    return client, http_client


# ============================================================================
# 1. 404 Auto-Unlock: Success Path
# ============================================================================

@pytest.mark.asyncio
async def test_read_notice_content_404_auto_unlock_success(tmp_path: Path) -> None:
    """Stress-test auto-unlock:
    - GET attach returns 404 on first request.
    - POST .../noticeboard/read/{evt}/{pub}/101 is called to unlock.
    - Retry GET attach returns 200 with PDF bytes.
    - read_notice_content returns marked_as_read=True and the PDF bytes.
    """
    pdf_payload = b"%PDF-1.4 Fake PDF stream for testing auto-unlock"
    calls: list[str] = []
    attach_get_count = 0

    notice_item = {
        "pubId": 888,
        "evtCode": "CF",
        "cntTitle": "Circolare n. 888 - Viaggio di Istruzione",
        "cntCategory": "Viaggi",
        "pubDT": "2026-11-09T08:00:00",
        "readStatus": False,
        "attachments": [{"attachNum": 1, "fileName": "viaggio.pdf"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attach_get_count
        path = request.url.path
        calls.append(f"{request.method} {path}")

        if path == "/rest/v1/students/1234567/noticeboard":
            return httpx.Response(200, json={"items": [notice_item]})

        if path == "/rest/v1/students/1234567/noticeboard/read/CF/888/101":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={"item": {"cntText": "Dettagli del viaggio di istruzione a Praga."}},
            )

        if path == "/rest/v1/students/1234567/noticeboard/attach/CF/888/1":
            assert request.method == "GET"
            attach_get_count += 1
            if attach_get_count == 1:
                # First request returns 404 (attachment locked)
                return httpx.Response(404, text="Attachment locked: notice unread")
            # Retry request returns 200 with PDF bytes
            return httpx.Response(
                200,
                content=pdf_payload,
                headers={"content-type": "application/pdf"},
            )

        raise AssertionError(f"Unexpected endpoint called: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF",
            pub_id="888",
            allow_mark_as_read=True,
        )

    # Verifications
    assert attach_get_count == 2, f"Expected exactly 2 GET attach attempts, got {attach_get_count}"
    assert result["skipped"] is False
    assert result["marked_as_read"] is True
    assert result["pdf_bytes"] == pdf_payload
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["attach_num"] == 1
    assert result["attachments"][0]["bytes"] == pdf_payload
    assert result["attachments"][0]["file_name"] == "viaggio.pdf"

    # Verify call sequence
    get_attach_indices = [i for i, c in enumerate(calls) if "GET" in c and "/attach/" in c]
    post_read_indices = [i for i, c in enumerate(calls) if "POST" in c and "/noticeboard/read/" in c]

    assert len(get_attach_indices) == 2
    # Verify that an unlock POST /read occurred between the 1st and 2nd GET /attach
    unlock_between = [idx for idx in post_read_indices if get_attach_indices[0] < idx < get_attach_indices[1]]
    assert len(unlock_between) >= 1, "Expected POST /noticeboard/read/ between 1st and 2nd GET /attach"


@pytest.mark.asyncio
async def test_read_notice_content_already_read_404_unlock(tmp_path: Path) -> None:
    """If notice is reported readStatus=True in noticeboard listing but attachment returns 404,
    auto-unlock must trigger, unlock via mark_notice_read, download PDF, and return marked_as_read=True.
    """
    pdf_payload = b"%PDF-1.4 Unlocked attachment bytes"
    attach_count = 0

    notice_item = {
        "pubId": 777,
        "evtCode": "CF",
        "cntTitle": "Circolare gia letta ma allegato 404",
        "readStatus": True,
        "attachments": [{"attachNum": 1, "fileName": "circolare.pdf"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attach_count
        path = request.url.path
        if path == "/rest/v1/students/1234567/noticeboard":
            return httpx.Response(200, json={"items": [notice_item]})
        if path == "/rest/v1/students/1234567/noticeboard/read/CF/777/101":
            return httpx.Response(200, json={"item": {"cntText": "Circolare 777"}})
        if path == "/rest/v1/students/1234567/noticeboard/attach/CF/777/1":
            attach_count += 1
            if attach_count == 1:
                return httpx.Response(404, text="Attachment locked")
            return httpx.Response(200, content=pdf_payload)
        raise AssertionError(f"Unexpected: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF",
            pub_id="777",
            allow_mark_as_read=False,  # Already read -> allowed
        )

    assert attach_count == 2
    assert result["marked_as_read"] is True
    assert result["pdf_bytes"] == pdf_payload


# ============================================================================
# 2. 404 Auto-Unlock: Double 404 & Failure Edge Cases
# ============================================================================

@pytest.mark.asyncio
async def test_read_notice_content_double_404_when_unlock_fails(tmp_path: Path) -> None:
    """Test scenario:
    1. GET attach returns 404.
    2. Unlock POST .../noticeboard/read/... returns 404 (unlock endpoint itself fails).
    Verify:
    - No infinite recursion.
    - No 2nd attempt to GET attach.
    - Handled cleanly without raising unhandled exception.
    - Returns pdf_bytes=b"" and empty attachments.
    """
    calls: list[str] = []

    notice_item = {
        "pubId": 555,
        "evtCode": "CF",
        "cntTitle": "Circolare bloccata in modo permanente",
        "readStatus": True,
        "attachments": [{"attachNum": 1, "fileName": "bloccato.pdf"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/rest/v1/students/1234567/noticeboard":
            return httpx.Response(200, json={"items": [notice_item]})
        if path == "/rest/v1/students/1234567/noticeboard/attach/CF/555/1":
            return httpx.Response(404, text="Attachment not found")
        if path == "/rest/v1/students/1234567/noticeboard/read/CF/555/101":
            # Unlock returns 404
            return httpx.Response(404, text="Read endpoint not found")
        raise AssertionError(f"Unexpected: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF",
            pub_id="555",
            allow_mark_as_read=False,
        )

    attach_calls = [c for c in calls if "/attach/" in c]
    # Must only have attempted attach ONCE, because unlock failed
    assert len(attach_calls) == 1
    assert result["pdf_bytes"] == b""
    assert result["attachments"] == []
    assert result["skipped"] is False


@pytest.mark.asyncio
async def test_read_notice_content_double_404_when_retry_still_404(tmp_path: Path) -> None:
    """Test scenario:
    1. GET attach returns 404.
    2. Unlock POST returns 200.
    3. Retry GET attach STILL returns 404.
    Verify:
    - Exactly 2 GET attach attempts (no infinite loop / 3rd attempt).
    - Exception caught cleanly in retry block.
    - Returns pdf_bytes=b"", attachments=[].
    """
    calls: list[str] = []

    notice_item = {
        "pubId": 666,
        "evtCode": "CF",
        "cntTitle": "Circolare con allegato inesistente",
        "readStatus": True,
        "attachments": [{"attachNum": 1, "fileName": "inesistente.pdf"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/rest/v1/students/1234567/noticeboard":
            return httpx.Response(200, json={"items": [notice_item]})
        if path == "/rest/v1/students/1234567/noticeboard/read/CF/666/101":
            return httpx.Response(200, json={"item": {"cntText": "Circolare 666"}})
        if path == "/rest/v1/students/1234567/noticeboard/attach/CF/666/1":
            # Returns 404 on ALL attempts
            return httpx.Response(404, text="Permanent 404")
        raise AssertionError(f"Unexpected: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        result = await client.read_notice_content(
            event_code="CF",
            pub_id="666",
            allow_mark_as_read=False,
        )

    attach_calls = [c for c in calls if "/attach/" in c]
    assert len(attach_calls) == 2, f"Expected exactly 2 attempts, got {len(attach_calls)}"
    assert result["pdf_bytes"] == b""
    assert result["attachments"] == []


@pytest.mark.asyncio
async def test_download_attachment_direct_double_404(tmp_path: Path) -> None:
    """Test _download_attachment(..., auto_unlock=True) directly:
    1. First attempt -> 404.
    2. Unlock POST -> 200.
    3. Second attempt -> 404 again.
    Must raise HTTPStatusError cleanly without recursion.
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if "/noticeboard/read/" in path:
            return httpx.Response(200, json={"status": "ok"})
        if "/noticeboard/attach/" in path:
            return httpx.Response(404, text="Not Found")
        raise AssertionError(f"Unexpected: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client._download_attachment(
                event_code="CF",
                pub_id="101",
                num=1,
                auto_unlock=True,
            )
        assert exc_info.value.response.status_code == 404

    attach_calls = [c for c in calls if "/attach/" in c]
    assert len(attach_calls) == 2


@pytest.mark.asyncio
async def test_download_attachment_direct_unlock_fails_404(tmp_path: Path) -> None:
    """Test _download_attachment(..., auto_unlock=True) when unlock returns 404:
    Must raise the original HTTPStatusError without making a second attach attempt.
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if "/noticeboard/read/" in path:
            return httpx.Response(404, text="Unlock failed 404")
        if "/noticeboard/attach/" in path:
            return httpx.Response(404, text="Attach 404")
        raise AssertionError(f"Unexpected: {request.method} {path}")

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client._download_attachment(
                event_code="CF",
                pub_id="101",
                num=1,
                auto_unlock=True,
            )
        assert exc_info.value.response.status_code == 404

    attach_calls = [c for c in calls if "/attach/" in c]
    assert len(attach_calls) == 1


# ============================================================================
# 3. SchoolBook Model & Client Edge Cases
# ============================================================================

def test_schoolbook_model_price_valid_formats() -> None:
    """Test SchoolBook model instantiation with standard valid types."""
    # 1. float
    b1 = SchoolBook(isbn="9788808123456", title="Matematica", price=29.90)
    assert b1.price == 29.90
    assert isinstance(b1.price, float)

    # 2. integer coerced to float
    b2 = SchoolBook(isbn="9788808123456", title="Matematica", price=30)
    assert b2.price == 30.0
    assert isinstance(b2.price, float)

    # 3. None explicitly passed
    b3 = SchoolBook(isbn="9788808123456", title="Matematica", price=None)
    assert b3.price is None

    # 4. missing price (default)
    b4 = SchoolBook(isbn="9788808123456", title="Matematica")
    assert b4.price is None


def test_schoolbook_model_price_comma_string_edge_case() -> None:
    """EMPIRICAL TEST: How does SchoolBook handle string '29,90' directly?
    In standard Pydantic v2, a float field rejects comma-separated string '29,90'
    unless a before-validator or pre-sanitizer is in place.
    """
    try:
        b = SchoolBook(isbn="9788808123456", title="Matematica", price="29,90")  # type: ignore[arg-type]
        # If validator exists and coerced it:
        assert b.price == 29.90
        direct_model_accepts_comma = True
    except ValidationError:
        # Standard Pydantic float parsing fails on '29,90'
        direct_model_accepts_comma = False

    # Document the empirical observation:
    # The raw Pydantic model rejects comma string directly if no validator is present.
    # Note: client.get_schoolbooks() handles this at the client layer before instantiating the model.
    print(f"SchoolBook direct comma parsing: {direct_model_accepts_comma}")


@pytest.mark.asyncio
async def test_client_get_schoolbooks_price_edge_cases(tmp_path: Path) -> None:
    """Empirically test that client.get_schoolbooks() correctly normalizes all price formats
    from raw API payloads:
    - '29,90' (Italian string with comma)
    - 29.90 (float)
    - 30 (integer)
    - None (null in JSON)
    - missing price key
    - malformed string 'GRATIS'
    """
    payload = {
        "schoolbooks": [
            {
                "isbn": "9788808123451",
                "title": "Libro String Comma",
                "subject": "Matematica",
                "price": "29,90",
            },
            {
                "isbn": "9788808123452",
                "title": "Libro Float",
                "subject": "Fisica",
                "price": 29.90,
            },
            {
                "isbn": "9788808123453",
                "title": "Libro Integer",
                "subject": "Chimica",
                "price": 30,
            },
            {
                "isbn": "9788808123454",
                "title": "Libro Prezzo None",
                "subject": "Storia",
                "prezzo": None,
            },
            {
                "isbn": "9788808123455",
                "title": "Libro Missing Price",
                "subject": "Filosofia",
            },
            {
                "isbn": "9788808123456",
                "title": "Libro Malformed Price",
                "subject": "Arte",
                "price": "GRATIS",
            },
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/students/1234567/schoolbooks"
        return httpx.Response(200, json=payload)

    client, http_client = _client(tmp_path, handler)
    async with http_client:
        books = await client.get_schoolbooks()

    assert len(books) == 6
    by_isbn = {b.isbn: b for b in books}

    # 1. '29,90' string with comma normalized to 29.90 float
    assert by_isbn["9788808123451"].price == 29.90
    assert isinstance(by_isbn["9788808123451"].price, float)

    # 2. 29.90 float preserved
    assert by_isbn["9788808123452"].price == 29.90

    # 3. 30 int normalized to 30.0 float
    assert by_isbn["9788808123453"].price == 30.0
    assert isinstance(by_isbn["9788808123453"].price, float)

    # 4. None -> None
    assert by_isbn["9788808123454"].price is None

    # 5. Missing price -> None
    assert by_isbn["9788808123455"].price is None

    # 6. Malformed string 'GRATIS' -> safely handled as None (no crash)
    assert by_isbn["9788808123456"].price is None
