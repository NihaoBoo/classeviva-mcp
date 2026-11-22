from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import httpx
import keyring
import pytest

from classeviva_mcp import classroom_auth
from classeviva_mcp.credentials import CredentialStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CredentialStore:
    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, key: vault.get((service, key)))
    monkeypatch.setattr(keyring, "set_password", lambda service, key, value: vault.__setitem__((service, key), value))
    monkeypatch.setattr(keyring, "delete_password", lambda service, key: vault.pop((service, key)))
    return CredentialStore(tmp_path)


@pytest.mark.asyncio
async def test_desktop_login_refresh_and_revoke(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = store.data_dir / "client.json"
    cfg.write_text(json.dumps({"installed": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "synthetic-client-secret",
        "auth_uri": "https://example.invalid",
        "token_uri": "https://example.invalid",
    }}))
    query: dict[str, list[str]] = {}
    operations: list[str] = []
    browser_tasks = []

    def fake_browser(url: str) -> bool:
        parsed = urlparse(url)
        assert parsed.netloc == "accounts.google.com"
        query.update(parse_qs(parsed.query))
        redirect = query["redirect_uri"][0]
        assert urlparse(redirect).hostname == "127.0.0.1"
        # The loopback listener must ignore an unrelated callback before the real one.
        def send() -> None:
            with pytest.raises(OSError):
                urlopen(redirect + "?code=unrelated&state=wrong", timeout=5)
            with urlopen(redirect + "?code=synthetic-code&state=" + query["state"][0], timeout=5) as response:
                assert response.status == 200

        browser_tasks.append(asyncio.create_task(asyncio.to_thread(send)))
        return True

    def google(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        assert not request.url.query
        form = parse_qs(request.content.decode())
        if request.url.path == "/revoke":
            assert form["token"] == ["synthetic-refresh"]
            operations.append("revoke")
            return httpx.Response(200)
        assert form["client_secret"] == ["synthetic-client-secret"]
        if form["grant_type"] == ["authorization_code"]:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).decode()
            assert query["code_challenge"] == [challenge.rstrip("=")]
            assert query["code_challenge_method"] == ["S256"]
            assert form["code"] == ["synthetic-code"]
            assert all("drive" not in scope for scope in query["scope"][0].split())
            operations.append("exchange")
            return httpx.Response(200, json={
                "access_token": "synthetic-first", "refresh_token": "synthetic-refresh",
                "scope": query["scope"][0], "token_type": "Bearer", "expires_in": 3600,
            })
        assert form["refresh_token"] == ["synthetic-refresh"]
        operations.append("refresh")
        return httpx.Response(200, json={
            "access_token": "synthetic-second", "token_type": "Bearer", "expires_in": 3600,
        })

    monkeypatch.setattr(classroom_auth.webbrowser, "open", fake_browser)
    monkeypatch.setattr(classroom_auth, "build_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(google), **kw
    ))
    with caplog.at_level(logging.DEBUG):
        await classroom_auth.connect(store, cfg)
        await asyncio.gather(*browser_tasks)
        broker = classroom_auth.ClassroomTokens()
        assert await broker.access_token(store) == "synthetic-second"
        assert await broker.access_token(store) == "synthetic-second"
        assert await classroom_auth.disconnect(store)
        assert await broker.access_token(store) == ""
    assert "synthetic-code" not in caplog.text
    assert "synthetic-refresh" not in caplog.text
    assert operations == ["exchange", "refresh", "revoke"]
    assert store.load_classroom_grant() == {}


@pytest.mark.asyncio
async def test_revoke_offline_still_erases_local_grant(store: CredentialStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.save_classroom_grant({"refresh_token": "synthetic-refresh"})

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret body omitted", request=request)

    monkeypatch.setattr(classroom_auth, "build_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(offline), **kw
    ))
    assert not await classroom_auth.disconnect(store)
    assert store.load_classroom_grant() == {}


def test_google_grant_never_uses_the_plaintext_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object) -> None:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(keyring, "set_password", unavailable)
    store = CredentialStore(tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False
    with pytest.raises(RuntimeError, match="Archivio sicuro"):
        store.save_classroom_grant({"refresh_token": "synthetic-refresh"})
    assert not list(tmp_path.iterdir())
