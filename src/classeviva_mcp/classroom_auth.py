"""Google desktop authorization; independent of the inbound MCP OAuth server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from classeviva_mcp.classroom_ical import _read_limited
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.network import build_async_client

SCOPES = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class ClassroomAuthError(RuntimeError):
    """Fixed, user-facing descriptions only; never attach provider bodies or URLs."""


class _CallbackServer(HTTPServer):
    timeout = 0.5
    state: str
    code: str | None = None
    denied = False

    def get_request(self) -> tuple[Any, Any]:
        sock, address = super().get_request()
        sock.settimeout(1.0)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # The default handler prints a traceback; callback data must stay private.


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_CallbackServer, self.server)
        try:
            parsed = urlsplit(self.path)
            if (
                parsed.path != "/oauth/callback" or parsed.netloc or parsed.scheme
                or self.headers.get_all("Host") != [f"127.0.0.1:{server.server_port}"]
            ):
                self.send_error(404)
                return
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            state = values.get("state", [])
            if len(state) != 1 or not secrets.compare_digest(state[0].encode(), server.state.encode()):
                raise ValueError
            code = values.get("code", [])
            errors = values.get("error", [])
            if len(errors) == 1 and errors[0] and not code:
                server.denied = True
            elif len(code) == 1 and 0 < len(code[0]) <= 4096 and not errors:
                server.code = code[0]
            else:
                raise ValueError
        except ValueError:
            self.send_error(400, "Invalid callback")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b"Return to the terminal to see the result. You can close this tab.")


def _desktop_client(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.read(32_769))
        client = data["installed"]
        client_id = client["client_id"]
        client_secret = client.get("client_secret", "")
        if (
            not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com")
            or not isinstance(client_secret, str) or len(client_id) > 256 or len(client_secret) > 256
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ClassroomAuthError("Serve il JSON di un client OAuth Google di tipo Desktop app.") from None
    return {"client_id": client_id, **({"client_secret": client_secret} if client_secret else {})}


async def _authorize(client_id: str, verifier: str) -> tuple[str, str]:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    with _CallbackServer(("127.0.0.1", 0), _CallbackHandler) as server:
        server.state = secrets.token_urlsafe(32)
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        query = urlencode({
            'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code',
            'scope': ' '.join(SCOPES), 'access_type': 'offline', 'prompt': 'select_account consent',
            'state': server.state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        })
        if not webbrowser.open(f"{AUTH_URL}?{query}"):
            raise ClassroomAuthError("Impossibile aprire il browser sul computer che esegue il comando.")
        deadline = time.monotonic() + 180
        while not server.code and not server.denied and time.monotonic() < deadline:
            await asyncio.to_thread(server.handle_request)
        if server.denied:
            raise ClassroomAuthError("Consenso non concesso. Se l'account è scolastico, verifica con l'amministratore.")
        if not server.code:
            raise ClassroomAuthError("Tempo scaduto: ripeti il login sul computer dove è aperto il browser.")
        return server.code, redirect


async def _exchange(form: dict[str, str]) -> dict[str, Any]:
    try:
        async with build_async_client(timeout=8, follow_redirects=False) as client, asyncio.timeout(12):
            async with client.stream("POST", TOKEN_URL, data=form) as response:
                if response.status_code in (400, 401, 403):
                    raise ClassroomAuthError("Google non ha autorizzato l'accesso. Riprova con `classroom login`.")
                response.raise_for_status()
                data = json.loads(await _read_limited(response, 32_768))
        if (
            not isinstance(data, dict) or str(data.get("token_type", "")).lower() != "bearer"
            or not isinstance(data.get("access_token"), str) or not data["access_token"]
            or not 0 < int(data.get("expires_in", 0)) <= 86_400
        ):
            raise ValueError
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise ClassroomAuthError("Google OAuth non disponibile o risposta non valida.") from None
    return data


def _scope_allowed(data: dict[str, Any], *, required: bool = False) -> bool:
    scopes = data.get("scope")
    if scopes is None:
        return not required
    return isinstance(scopes, str) and set(scopes.split()) == set(SCOPES)


async def connect(store: CredentialStore, path: Path) -> None:
    client = _desktop_client(path)
    store.probe_storage()
    if store.load_classroom_grant():
        raise ClassroomAuthError("Google è già collegato. Esegui prima `classeviva-mcp classroom logout`.")
    generation = store.classroom_generation()
    verifier = secrets.token_urlsafe(64)
    code, redirect = await _authorize(client["client_id"], verifier)
    data = await _exchange({
        **client, "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect, "code_verifier": verifier,
    })
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise ClassroomAuthError("Google non ha rilasciato un'autorizzazione rinnovabile.")
    try:
        if not _scope_allowed(data, required=True):
            raise ClassroomAuthError("Concedi solo i permessi Classroom richiesti; non è stata salvata alcuna autorizzazione.")
        if generation != store.classroom_generation() or store.load_classroom_grant():
            raise ClassroomAuthError("Configurazione cambiata durante il login. Riprova.")
        store.save_classroom_grant({**client, "refresh_token": refresh})
    except Exception:
        if not await _revoke(refresh):
            raise ClassroomAuthError("Login non salvato. Revoca l'accesso residuo dalle impostazioni Google.") from None
        raise


async def _revoke(refresh: str) -> bool:
    if not refresh:
        return True
    try:
        async with build_async_client(timeout=8, follow_redirects=False) as client, asyncio.timeout(12):
            async with client.stream("POST", REVOKE_URL, data={"token": refresh}) as response:
                return response.status_code == 200
    except (httpx.HTTPError, TimeoutError):
        return False


async def disconnect(store: CredentialStore, *, purge_all: bool | None = None) -> bool:
    try:
        refresh = store.load_classroom_grant().get("refresh_token")
    except Exception:
        refresh = None
        readable = False
    else:
        readable = True
    try:
        if purge_all is None:
            store.clear_classroom()
        else:
            store.clear(purge_all=purge_all)
    finally:
        revoked = await _revoke(refresh if isinstance(refresh, str) else "")
    return readable and revoked


class ClassroomTokens:
    """Access tokens live only in RAM, and are usable only while the grant exists."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._grant: dict[str, Any] = {}
        self._access = ""
        self._expires_at = 0.0

    async def access_token(self, store: CredentialStore) -> str:
        async with self._lock:
            grant = store.load_classroom_grant()
            if grant != self._grant or not grant:
                self._grant, self._access, self._expires_at = {}, "", 0.0
            if not grant:
                return ""
            if self._access and time.monotonic() < self._expires_at:
                return self._access
            form: dict[str, str] = {
                key: grant[key] for key in ("client_id", "client_secret", "refresh_token")
                if isinstance(grant.get(key), str)
            }
            if not all(isinstance(form.get(key), str) and form[key] for key in ("client_id", "refresh_token")):
                raise ClassroomAuthError("Dati Google non validi; scollega e ricollega Classroom.")
            data = await _exchange({**form, "grant_type": "refresh_token"})
            if store.load_classroom_grant() != grant:
                raise ClassroomAuthError("Autorizzazione Google cambiata durante la richiesta. Riprova.")
            if not _scope_allowed(data):
                raise ClassroomAuthError("Permessi Google cambiati; scollega e ricollega Classroom.")
            self._grant = grant
            self._access = data["access_token"]
            self._expires_at = time.monotonic() + max(0, int(data["expires_in"]) - 60)
            return self._access
