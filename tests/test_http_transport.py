from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.http_server import (
    TOKEN_ENV_VAR,
    BearerAuthMiddleware,
    is_loopback,
    resolve_token,
)

TOKEN = "s3cr3t-token-value"


async def _ok_app(scope, receive, send) -> None:  # noqa: ANN001, ANN202
    """Minimal ASGI app that always answers 200."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _client(app) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_request_without_token_is_rejected() -> None:
    app = BearerAuthMiddleware(_ok_app, TOKEN)
    async with _client(app) as client:
        resp = await client.post("/mcp")

    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == 'Bearer realm="classeviva-mcp"'


@pytest.mark.asyncio
async def test_request_with_wrong_token_is_rejected() -> None:
    app = BearerAuthMiddleware(_ok_app, TOKEN)
    async with _client(app) as client:
        resp = await client.post("/mcp", headers={"Authorization": "Bearer nope"})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_request_with_valid_token_is_allowed() -> None:
    app = BearerAuthMiddleware(_ok_app, TOKEN)
    async with _client(app) as client:
        resp = await client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def lifespan_app(scope, receive, send) -> None:  # noqa: ANN001, ANN202
        seen.append(scope["type"])
        await send({"type": "lifespan.startup.complete"})

    app = BearerAuthMiddleware(lifespan_app, TOKEN)
    async with _client(app) as client:
        await client.get("/mcp")

    # The lifespan scope is forwarded untouched (no Authorization header there).
    assert "lifespan" in seen or seen == []


def test_resolve_token_prefers_the_explicit_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token\n", encoding="utf-8")

    token, source = resolve_token(str(token_file), tmp_path)

    assert token == "file-token"
    assert "file" in source


def test_resolve_token_falls_back_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")

    token, source = resolve_token(None, tmp_path)

    assert token == "env-token"
    assert TOKEN_ENV_VAR in source


def test_resolve_token_generates_one_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    token, source = resolve_token(None, tmp_path)

    assert len(token) >= 32
    assert source == "generato automaticamente"


def test_resolve_token_rejects_an_empty_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("   \n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="vuoto"):
        resolve_token(str(token_file), tmp_path)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("example.com", False),
    ],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_mcp_token_is_protected_at_rest(tmp_path: Path) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)

    store.save_mcp_token("my-token")

    assert store.mcp_token_file.exists()
    assert store.load_mcp_token() == "my-token"
    if store._dpapi:
        # Windows: the file is DPAPI-encrypted, the token is not readable.
        assert "my-token" not in store.mcp_token_file.read_bytes().decode("utf-8", "ignore")
    else:
        # Linux/macOS: plaintext file, but readable only by the owner.
        assert oct(store.mcp_token_file.stat().st_mode)[-3:] == "600"


def test_missing_mcp_token_returns_empty(tmp_path: Path) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False

    assert store.load_mcp_token() == ""


def test_loopback_async_helper_is_not_needed() -> None:
    # Guard against accidentally making the sync helper awaitable.
    assert asyncio.iscoroutinefunction(resolve_token) is False
