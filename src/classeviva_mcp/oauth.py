"""Self-hosted, single-owner OAuth 2.1 authorization server for remote access.

Why OAuth and not a static token: the hosted MCP clients that people use from
a phone (Claude web/mobile, ChatGPT) connect to a remote server from their own
cloud and only support OAuth for personal accounts. Static request headers are
an organisation-only beta in Claude and are absent from ChatGPT.

Design goals: zero external services, zero maintenance, safe defaults.

* The OAuth machinery (metadata, PKCE S256, dynamic client registration,
  token endpoint, revocation) comes from the official MCP Python SDK; this
  module only implements the *provider*: the owner's consent and the storage.
* There is exactly one resource owner: the person who ran ``remote-setup``.
  Consent is granted by typing the **owner passphrase** chosen during
  ``remote-setup`` (never printed) on a small HTML page served by this server. Only its
  scrypt hash is stored.
* Clients identify themselves with Dynamic Client Registration (RFC 7591) or
  with a Client ID Metadata Document (CIMD, the method preferred by the latest
  MCP authorization spec). CIMD documents are fetched only from an allowlist
  of hosts (``claude.ai``, ``chatgpt.com`` by default) to avoid SSRF.
* Access tokens live 1 hour, refresh tokens 30 days and are rotated on every
  use; reusing a rotated refresh token revokes the whole grant. Tokens are
  stored only as SHA-256 hashes, in the private, DPAPI/0600-protected data dir.
* Wrong passphrases trigger an exponential lockout.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, ValidationError

from classeviva_mcp.network import build_async_client

logger = logging.getLogger(__name__)

SCOPE = "classeviva"
CONSENT_PATH = "/oauth/consent"
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
AUTH_CODE_TTL = 300
PENDING_TTL = 600
MAX_CLIENTS = 50
MAX_GRANTS = 50
CIMD_MAX_BYTES = 16 * 1024
CIMD_CACHE_TTL = 3600
DEFAULT_CIMD_HOSTS = ("claude.ai", "chatgpt.com")
LOCKOUT_FREE_ATTEMPTS = 5
LOCKOUT_BASE_SECONDS = 60
LOCKOUT_MAX_SECONDS = 900
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

StateReader = Callable[[], dict[str, Any]]
StateWriter = Callable[[dict[str, Any]], None]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_passphrase(passphrase: str, salt: bytes | None = None) -> dict[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, **_SCRYPT)
    return {"salt": salt.hex(), "hash": digest.hex()}


def verify_passphrase(passphrase: str, record: dict[str, str]) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
    except (KeyError, ValueError):
        return False
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, **_SCRYPT)
    return hmac.compare_digest(digest, expected)


MIN_OWNER_PHRASE_CHARS = 12

# Fixed messages (never derived from the secret) for each weakness code.
OWNER_PHRASE_MESSAGES = {
    "short": f"la passphrase deve avere almeno {MIN_OWNER_PHRASE_CHARS} caratteri.",
    "repetitive": "la passphrase è troppo ripetitiva.",
    "spaces": "la passphrase non può iniziare o finire con spazi.",
}


def owner_passphrase_problem(passphrase: str) -> str | None:
    """Returns a weakness code (key of ``OWNER_PHRASE_MESSAGES``) or None.

    Brute force is throttled by the consent lockout, but the passphrase guards
    the whole registry from the Internet, so trivial values are refused.
    """
    if len(passphrase) < MIN_OWNER_PHRASE_CHARS:
        return "short"
    if len(set(passphrase.lower())) < 6:
        return "repetitive"
    if passphrase.strip() != passphrase:
        return "spaces"
    return None


def generate_passphrase() -> str:
    """Random owner passphrase (~120 bits), grouped to be easy to type on a phone."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(24))
    return "-".join(raw[i : i + 6] for i in range(0, 24, 6))


def _is_loopback_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")


class LocalClient(OAuthClientInformationFull):
    """Client record with RFC 8252 port-agnostic matching for loopback redirects."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and _is_loopback_redirect(str(redirect_uri)):
            req = urlparse(str(redirect_uri))
            for registered in self.redirect_uris or []:
                reg = urlparse(str(registered))
                if (
                    _is_loopback_redirect(str(registered))
                    and reg.hostname == req.hostname
                    and (reg.path or "/") == (req.path or "/")
                ):
                    return redirect_uri
            raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")
        return super().validate_redirect_uri(redirect_uri)


@dataclass
class _Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    expires_at: float


def cimd_allowed_hosts() -> set[str]:
    extra = os.getenv("CLASSEVIVA_OAUTH_CIMD_HOSTS", "")
    hosts = {h.strip().lower() for h in extra.split(",") if h.strip()}
    return hosts | set(DEFAULT_CIMD_HOSTS)


class LocalOAuthProvider:
    """``OAuthAuthorizationServerProvider`` for a single owner, persisted locally."""

    def __init__(
        self,
        *,
        read_state: StateReader,
        write_state: StateWriter,
        public_url: str,
        resource_url: str,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._read = read_state
        self._write = write_state
        self.public_url = public_url.rstrip("/")
        self.resource_url = resource_url
        self._http_factory = http_client_factory or (
            lambda: build_async_client(timeout=5.0, follow_redirects=False)
        )
        self._now = clock
        self._lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._cimd_cache: dict[str, tuple[float, LocalClient]] = {}
        self._failures = 0
        self._locked_until = 0.0

    # --- persistence ---------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            state = self._read()
        except Exception:  # noqa: BLE001 - missing/corrupt state starts empty
            state = {}
        state.setdefault("clients", {})
        state.setdefault("grants", {})
        return state

    def has_owner(self) -> bool:
        return bool(self._state().get("owner"))

    # --- clients -------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id.startswith("https://"):
            return await self._get_cimd_client(client_id)
        raw = self._state()["clients"].get(client_id)
        if raw is None:
            return None
        try:
            return LocalClient.model_validate(raw)
        except ValidationError:
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id mancante")
        async with self._lock:
            state = self._state()
            clients: dict[str, Any] = state["clients"]
            record = client_info.model_dump(mode="json", exclude_none=True)
            record["_registered_at"] = self._now()
            clients[client_info.client_id] = record
            # DCR creates a new client on every fresh connection: keep the newest.
            if len(clients) > MAX_CLIENTS:
                oldest = sorted(clients, key=lambda k: clients[k].get("_registered_at", 0))
                for key in oldest[: len(clients) - MAX_CLIENTS]:
                    clients.pop(key, None)
            self._write(state)

    async def _get_cimd_client(self, client_id: str) -> LocalClient | None:
        cached = self._cimd_cache.get(client_id)
        if cached and cached[0] > self._now():
            return cached[1]
        parsed = urlparse(client_id)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.port not in (None, 443) or host not in cimd_allowed_hosts():
            logger.warning("CIMD rifiutato per host non consentito: %s", host or "?")
            return None
        try:
            async with self._http_factory() as client:
                async with client.stream("GET", client_id, headers={"Accept": "application/json"}) as resp:
                    if resp.status_code != 200:
                        return None
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > CIMD_MAX_BYTES:
                            return None
            data = json.loads(body)
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("client_id") != client_id:
            return None
        redirect_uris = data.get("redirect_uris") or []
        if not redirect_uris:
            return None
        try:
            record = LocalClient(
                client_id=client_id,
                client_name=str(data.get("client_name") or host)[:100],
                redirect_uris=redirect_uris,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=SCOPE,
            )
        except ValidationError:
            return None
        self._cimd_cache[client_id] = (self._now() + CIMD_CACHE_TTL, record)
        return record

    # --- authorization ---------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.has_owner():
            raise AuthorizeError(
                error="access_denied",
                error_description="Server non configurato: esegui `classeviva-mcp remote-setup`.",
            )
        if params.resource and params.resource.rstrip("/") != self.resource_url.rstrip("/"):
            raise AuthorizeError(error="invalid_request", error_description="resource non valido")
        self._prune()
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = _Pending(
            client_id=client.client_id or "",
            client_name=(client.client_name or client.client_id or "client")[:100],
            params=params,
            expires_at=self._now() + PENDING_TTL,
        )
        return f"{self.public_url}{CONSENT_PATH}?request={request_id}"

    def _prune(self) -> None:
        now = self._now()
        for key in [k for k, v in self._pending.items() if v.expires_at < now]:
            self._pending.pop(key, None)
        for key in [k for k, v in self._codes.items() if v.expires_at < now]:
            self._codes.pop(key, None)

    def lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - self._now()))

    def describe_pending(self, request_id: str) -> _Pending | None:
        self._prune()
        return self._pending.get(request_id)

    def approve(self, request_id: str, passphrase: str) -> str:
        """Checks the owner passphrase and returns the redirect URL with the code.

        Raises ``PermissionError`` on a wrong passphrase or lockout and
        ``LookupError`` for an unknown/expired request.
        """
        self._prune()
        if self.lockout_remaining():
            raise PermissionError("locked")
        pending = self._pending.get(request_id)
        if pending is None:
            raise LookupError("expired")
        owner = self._state().get("owner") or {}
        if not verify_passphrase(passphrase, owner):
            self._failures += 1
            if self._failures >= LOCKOUT_FREE_ATTEMPTS:
                exponent = self._failures - LOCKOUT_FREE_ATTEMPTS
                self._locked_until = self._now() + min(LOCKOUT_MAX_SECONDS, LOCKOUT_BASE_SECONDS * 2**exponent)
            raise PermissionError("wrong")
        self._failures = 0
        self._pending.pop(request_id, None)
        code = secrets.token_urlsafe(32)
        params = pending.params
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=self._now() + AUTH_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource_url,
            subject="owner",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, request_id: str) -> str | None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return None
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            error="access_denied",
            error_description="Accesso negato dal proprietario",
            state=pending.params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._prune()
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="codice già usato o scaduto")
        async with self._lock:
            state = self._state()
            grant_id = secrets.token_urlsafe(12)
            token = self._issue(state, grant_id, client.client_id or "")
            grants: dict[str, Any] = state["grants"]
            if len(grants) > MAX_GRANTS:
                oldest = sorted(grants, key=lambda k: grants[k].get("created_at", 0))
                for key in oldest[: len(grants) - MAX_GRANTS]:
                    grants.pop(key, None)
            self._write(state)
        return token

    def _issue(self, state: dict[str, Any], grant_id: str, client_id: str) -> OAuthToken:
        now = int(self._now())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        grant = state["grants"].get(grant_id) or {"client_id": client_id, "created_at": now, "used_refresh": []}
        previous = grant.get("refresh_hash")
        if previous:
            # Remember rotated refresh tokens to detect replay (OAuth 2.1 §4.3.1).
            grant["used_refresh"] = ([previous] + grant.get("used_refresh", []))[:20]
        grant.update(
            access_hash=_sha256(access),
            access_expires_at=now + ACCESS_TOKEN_TTL,
            refresh_hash=_sha256(refresh),
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
            last_used_at=now,
        )
        state["grants"][grant_id] = grant
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL,
            scope=SCOPE,
            refresh_token=refresh,
        )

    def _find_grant(self, state: dict[str, Any], field: str, token: str) -> tuple[str, dict[str, Any]] | None:
        digest = _sha256(token)
        for grant_id, grant in state["grants"].items():
            stored = grant.get(field)
            if stored and hmac.compare_digest(stored, digest):
                return grant_id, grant
        return None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        state = self._state()
        found = self._find_grant(state, "refresh_hash", refresh_token)
        if found is None:
            digest = _sha256(refresh_token)
            for grant_id, grant in list(state["grants"].items()):
                if digest in grant.get("used_refresh", []):
                    logger.warning("Refresh token riutilizzato: revoco la concessione.")
                    async with self._lock:
                        fresh = self._state()
                        fresh["grants"].pop(grant_id, None)
                        self._write(fresh)
            return None
        _grant_id, grant = found
        if grant.get("client_id") != client.client_id or grant.get("refresh_expires_at", 0) < self._now():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=[SCOPE],
            expires_at=grant.get("refresh_expires_at"),
            resource=self.resource_url,
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        async with self._lock:
            state = self._state()
            found = self._find_grant(state, "refresh_hash", refresh_token.token)
            if found is None:
                raise TokenError(error="invalid_grant", error_description="refresh token non valido")
            grant_id, _grant = found
            token = self._issue(state, grant_id, client.client_id or "")
            self._write(state)
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        state = self._state()
        found = self._find_grant(state, "access_hash", token)
        if found is None:
            return None
        _grant_id, grant = found
        expires_at = int(grant.get("access_expires_at", 0))
        if expires_at < self._now():
            return None
        return AccessToken(
            token=token,
            client_id=str(grant.get("client_id", "")),
            scopes=[SCOPE],
            expires_at=expires_at,
            resource=self.resource_url,
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._lock:
            state = self._state()
            for field in ("access_hash", "refresh_hash"):
                found = self._find_grant(state, field, token.token)
                if found:
                    state["grants"].pop(found[0], None)
                    self._write(state)
                    return


# --- owner management (CLI) --------------------------------------------------


def set_owner_passphrase(read_state: StateReader, write_state: StateWriter, passphrase: str) -> None:
    """Stores the owner passphrase hash and revokes every existing grant/client."""
    try:
        state = read_state()
    except Exception:  # noqa: BLE001
        state = {}
    state["owner"] = hash_passphrase(passphrase)
    state["grants"] = {}
    state["clients"] = {}
    write_state(state)


def revoke_all(read_state: StateReader, write_state: StateWriter) -> int:
    try:
        state = read_state()
    except Exception:  # noqa: BLE001
        return 0
    count = len(state.get("grants") or {})
    state["grants"] = {}
    state["clients"] = {}
    write_state(state)
    return count


# --- consent page --------------------------------------------------------------

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'",
}


def _page(body: str, status: int = 200) -> Any:
    from starlette.responses import HTMLResponse

    doc = (
        "<!doctype html><html lang='it'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ClasseViva MCP</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem;"
        "color:#111;background:#fff}@media(prefers-color-scheme:dark){body{color:#eee;background:#111}}"
        "input,button{font-size:1rem;padding:.6rem;width:100%;box-sizing:border-box;margin:.3rem 0}"
        ".muted{opacity:.7;font-size:.9rem}code{word-break:break-all}</style></head><body>"
        f"{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_PAGE_HEADERS)


def consent_routes(provider: LocalOAuthProvider) -> list[Any]:
    from starlette.requests import Request
    from starlette.responses import RedirectResponse, Response
    from starlette.routing import Route

    async def consent(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = provider.describe_pending(request_id)
            if pending is None:
                return _page("<h1>Richiesta scaduta</h1><p>Riprova a collegare il connettore.</p>", 400)
            redirect_host = html.escape(urlparse(str(pending.params.redirect_uri)).hostname or "?")
            loopback = _is_loopback_redirect(str(pending.params.redirect_uri))
            warn = (
                "<p><b>Attenzione:</b> il ritorno avviene su questo computer (localhost): "
                "approva solo se hai avviato tu il collegamento.</p>"
                if loopback
                else ""
            )
            return _page(
                "<h1>Collegare ClasseViva?</h1>"
                f"<p><b>{html.escape(pending.client_name)}</b> chiede accesso in lettura al tuo "
                "registro ClasseViva tramite il tuo server personale.</p>"
                f"<p class='muted'>Ritorno a: <code>{redirect_host}</code></p>{warn}"
                "<form method='post'>"
                f"<input type='hidden' name='request' value='{html.escape(request_id)}'>"
                "<label>Passphrase del proprietario (da <code>remote-setup</code>)"
                "<input type='password' name='passphrase' autocomplete='current-password' required "
                "autofocus></label>"
                "<button name='action' value='approve'>Consenti</button>"
                "<button name='action' value='deny'>Nega</button></form>"
            )

        form = await request.form()
        request_id = str(form.get("request", ""))
        if str(form.get("action", "")) == "deny":
            target = provider.deny(request_id)
            if target:
                return RedirectResponse(target, status_code=303)
            return _page("<h1>Richiesta scaduta</h1>", 400)
        try:
            target = await asyncio.to_thread(provider.approve, request_id, str(form.get("passphrase", "")))
        except LookupError:
            return _page("<h1>Richiesta scaduta</h1><p>Riprova a collegare il connettore.</p>", 400)
        except PermissionError:
            wait = provider.lockout_remaining()
            msg = f"Troppi tentativi: riprova tra {wait} secondi." if wait else "Passphrase errata."
            return _page(
                f"<h1>Accesso negato</h1><p>{msg}</p>"
                f"<p><a href='{CONSENT_PATH}?request={html.escape(request_id)}'>Riprova</a></p>",
                403 if not wait else 429,
            )
        return RedirectResponse(target, status_code=303)

    return [Route(CONSENT_PATH, endpoint=consent, methods=["GET", "POST"])]


def metadata_patch_middleware(app: Any) -> Any:
    """Advertises public clients and CIMD in the AS metadata served by the SDK.

    The SDK metadata lists only ``client_secret_*`` auth methods. Claude picks
    CIMD only when ``none`` and ``client_id_metadata_document_supported`` are
    advertised; spec-compliant clients use both.
    """

    async def wrapper(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/.well-known/oauth-authorization-server":
            await app(scope, receive, send)
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def capture(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    body = b"".join(chunks)
                    if start.get("status") == 200:
                        try:
                            data = json.loads(body)
                            methods = data.get("token_endpoint_auth_methods_supported") or []
                            if "none" not in methods:
                                data["token_endpoint_auth_methods_supported"] = [*methods, "none"]
                            data["client_id_metadata_document_supported"] = True
                            body = json.dumps(data).encode()
                        except ValueError:
                            pass
                    headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(body)).encode()))
                    await send({**start, "headers": headers})
                    await send({"type": "http.response.body", "body": body})

        await app(scope, receive, capture)

    return wrapper

