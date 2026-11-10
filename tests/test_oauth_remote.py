"""End-to-end tests for the self-hosted OAuth remote mode."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from classeviva_mcp import oauth
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.http_server import build_app, normalize_public_url, resolve_token
from classeviva_mcp.server import mcp

PUBLIC = "https://pc.tail1234.ts.net"
RESOURCE = f"{PUBLIC}/mcp"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
PASSPHRASE = "abcdef-ghjkmn-pqrstu-vwxyz2"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture
def setup(tmp_path: Path) -> tuple[TestClient, oauth.LocalOAuthProvider, CredentialStore]:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    oauth.set_owner_passphrase(store.load_oauth_state, store.save_oauth_state, PASSPHRASE)
    provider = oauth.LocalOAuthProvider(
        read_state=store.load_oauth_state,
        write_state=store.save_oauth_state,
        public_url=PUBLIC,
        resource_url=RESOURCE,
    )
    app = build_app(mcp, path="/mcp", public_url=PUBLIC, oauth_provider=provider)
    client = TestClient(app, base_url=PUBLIC, follow_redirects=False)
    return client, provider, store


def _register(client: TestClient) -> dict[str, Any]:
    resp = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [CLAUDE_CB],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _authorize(client: TestClient, client_id: str, challenge: str, passphrase: str = PASSPHRASE) -> httpx.Response:
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CB,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": oauth.SCOPE,
            "resource": RESOURCE,
        },
    )
    assert resp.status_code == 302, resp.text
    consent_url = urlparse(resp.headers["location"])
    assert consent_url.path == oauth.CONSENT_PATH
    request_id = parse_qs(consent_url.query)["request"][0]
    page = client.get(oauth.CONSENT_PATH, params={"request": request_id})
    assert page.status_code == 200
    assert "Claude" in page.text and "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    return client.post(
        oauth.CONSENT_PATH, data={"request": request_id, "passphrase": passphrase, "action": "approve"}
    )


def _full_flow(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    registered = _register(client)
    verifier, challenge = _pkce()
    approved = _authorize(client, registered["client_id"], challenge)
    assert approved.status_code == 303
    target = urlparse(approved.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == CLAUDE_CB
    query = parse_qs(target.query)
    assert query["state"] == ["xyz"]
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CLAUDE_CB,
            "client_id": registered["client_id"],
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )
    assert token.status_code == 200, token.text
    return registered, token.json()


def test_metadata_advertises_pkce_public_clients_and_cimd(setup: Any) -> None:
    client, _provider, _store = setup
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["registration_endpoint"] == f"{PUBLIC}/register"
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE
    assert prm["authorization_servers"][0].rstrip("/") == PUBLIC


def test_unauthenticated_mcp_call_returns_401_with_resource_metadata(setup: Any) -> None:
    client, _provider, _store = setup
    resp = client.post("/mcp", json={})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


def test_full_oauth_flow_then_mcp_initialize(setup: Any) -> None:
    client, _provider, store = setup
    with client:
        _registered, token = _full_flow(client)
        assert token["token_type"] == "Bearer"
        assert token["expires_in"] == oauth.ACCESS_TOKEN_TTL
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert "classeviva-mcp" in resp.text
    # Only hashes are persisted, never the raw tokens.
    raw = json.dumps(store.load_oauth_state())
    assert token["access_token"] not in raw
    assert token["refresh_token"] not in raw
    assert PASSPHRASE not in raw


def test_refresh_rotates_and_replay_revokes_grant(setup: Any) -> None:
    client, _provider, _store = setup
    registered, token = _full_flow(client)
    first = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": registered["client_id"]},
    )
    assert first.status_code == 200, first.text
    rotated = first.json()
    assert rotated["refresh_token"] != token["refresh_token"]
    # Reusing the old refresh token is refused with invalid_grant and kills the grant.
    replay = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": registered["client_id"]},
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    after = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": rotated["refresh_token"],
            "client_id": registered["client_id"],
        },
    )
    assert after.status_code == 400


def test_wrong_passphrase_is_rejected_and_locks_out(setup: Any) -> None:
    client, provider, _store = setup
    registered = _register(client)
    _verifier, challenge = _pkce()
    for _ in range(oauth.LOCKOUT_FREE_ATTEMPTS):
        denied = _authorize(client, registered["client_id"], challenge, passphrase="sbagliata")
        assert denied.status_code in (403, 429)
        assert "location" not in denied.headers
    assert provider.lockout_remaining() > 0
    # Even the right passphrase is refused during the lockout.
    locked = _authorize(client, registered["client_id"], challenge)
    assert locked.status_code == 429


def test_code_cannot_be_used_twice_or_without_pkce(setup: Any) -> None:
    client, _provider, _store = setup
    registered = _register(client)
    verifier, challenge = _pkce()
    approved = _authorize(client, registered["client_id"], challenge)
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CB, "client_id": registered["client_id"]}
    bad = client.post("/token", data={**base, "code_verifier": "x" * 50})
    assert bad.status_code == 400
    ok = client.post("/token", data={**base, "code_verifier": verifier})
    assert ok.status_code == 200
    again = client.post("/token", data={**base, "code_verifier": verifier})
    assert again.status_code == 400


def test_foreign_host_header_is_rejected_even_with_valid_token(setup: Any) -> None:
    """DNS-rebinding protection stays on and accepts only loopback + the public host."""
    client, _provider, _store = setup
    with client:
        _registered, token = _full_flow(client)
        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        }
        evil = client.post("/mcp", json=body, headers={**headers, "Host": "evil.example.com"})
        assert evil.status_code == 421
        for route in ("/register", "/.well-known/oauth-authorization-server", oauth.CONSENT_PATH):
            assert client.get(route, headers={"Host": "evil.example.com"}).status_code == 421
        good = client.post("/mcp", json=body, headers=headers)
        assert good.status_code == 200


def test_revoke_all_invalidates_tokens(setup: Any) -> None:
    client, provider, store = setup
    _registered, token = _full_flow(client)
    assert oauth.revoke_all(store.load_oauth_state, store.save_oauth_state) == 1
    import asyncio

    assert asyncio.run(provider.load_access_token(token["access_token"])) is None


@pytest.mark.asyncio
async def test_cimd_only_from_allowlisted_hosts(tmp_path: Path) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    doc_url = "https://claude.ai/oauth/mcp-client-metadata"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": doc_url,
                "client_name": "Claude",
                "redirect_uris": [CLAUDE_CB, "http://localhost/callback"],
            },
        )

    provider = oauth.LocalOAuthProvider(
        read_state=store.load_oauth_state,
        write_state=store.save_oauth_state,
        public_url=PUBLIC,
        resource_url=RESOURCE,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = await provider.get_client(doc_url)
    assert client is not None
    assert client.token_endpoint_auth_method == "none"
    # RFC 8252: loopback redirects match on any port.
    from pydantic import AnyUrl

    assert client.validate_redirect_uri(AnyUrl("http://localhost:53123/callback"))
    assert await provider.get_client("https://evil.example.com/meta.json") is None
    assert await provider.get_client("http://claude.ai/meta.json") is None


def test_public_url_validation() -> None:
    assert normalize_public_url("https://pc.tail1.ts.net/") == "https://pc.tail1.ts.net"
    for bad in ("http://pc.example.com", "https://pc.example.com/mcp", "pc.example.com"):
        with pytest.raises(ValueError):
            normalize_public_url(bad)


def test_stored_bearer_token_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the token changed on every restart, disconnecting clients."""
    monkeypatch.delenv("CLASSEVIVA_MCP_TOKEN", raising=False)
    token, source = resolve_token(None, tmp_path, stored_token="saved-token")
    assert (token, source) == ("saved-token", "salvato")


def test_passphrase_hashing_roundtrip() -> None:
    record = oauth.hash_passphrase("segreta")
    assert oauth.verify_passphrase("segreta", record)
    assert not oauth.verify_passphrase("altra", record)
    phrase = oauth.generate_passphrase()
    assert len(phrase) == 27 and phrase.count("-") == 3


def test_token_mode_requires_bearer_and_known_host() -> None:
    app = build_app(mcp, "tok-123", path="/mcp")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers={"Host": "evil.example.com"}).status_code == 421
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_owner_passphrase_strength() -> None:
    assert oauth.owner_passphrase_problem("corto") is not None
    assert oauth.owner_passphrase_problem("aaaaaaaaaaaaaaa") is not None
    assert oauth.owner_passphrase_problem(" spazio iniziale ok ") is not None
    assert oauth.owner_passphrase_problem("cavallo batteria graffetta") is None
