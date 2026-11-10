"""HTTP transport for remote use (phone apps, scheduled AI tasks, bots).

The default transport is still stdio (the client spawns the server as a child
process). ``serve --transport streamable-http`` exposes the same tools over
HTTP with one of two authentication modes:

* ``oauth`` (default when ``--public-url`` is given): a self-hosted OAuth 2.1
  authorization server for a single owner (see :mod:`classeviva_mcp.oauth`).
  This is what Claude web/mobile and ChatGPT need for personal connectors.
* ``token``: a static bearer token, for clients that let you set a header
  (Claude Code, Codex, scripts, bots).

Security model:

* Authentication is **always required**; there is no anonymous mode.
* The server binds to loopback by default and is meant to be published
  through an HTTPS tunnel (Tailscale Funnel, Cloudflare Tunnel). Binding to
  a routable address requires ``--allow-lan`` because HTTP is not encrypted.
* ``Host``/``Origin`` checks (DNS-rebinding protection) stay on and accept
  the loopback names plus the host of ``--public-url``.
* Tokens are 256-bit random values: brute force is not a practical threat, so
  wrong tokens are simply rejected (no global lockout an attacker could abuse
  to disconnect the owner).
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TOKEN_ENV_VAR = "CLASSEVIVA_MCP_TOKEN"
TOKEN_FILENAME = "mcp_token"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})
TOKEN_BYTES = 32


def is_loopback(host: str) -> bool:
    """True when the host is a loopback address (only reachable from this PC)."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def resolve_token(token_file: str | None, data_dir: Path, stored_token: str = "") -> tuple[str, str]:
    """Returns the bearer token to use and a human-readable source description.

    Order: explicit ``--token-file`` -> ``CLASSEVIVA_MCP_TOKEN`` -> the token
    saved by a previous run -> a newly generated one (the caller saves it).
    """
    if token_file:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Impossibile leggere il token da {path}: {exc}") from exc
        if not token:
            raise RuntimeError(f"Il file del token {path} e' vuoto.")
        return token, f"file {path}"

    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token, f"variabile d'ambiente {TOKEN_ENV_VAR}"

    if stored_token:
        return stored_token, "salvato"

    generated = secrets.token_urlsafe(TOKEN_BYTES)
    return generated, "generato automaticamente"


def normalize_public_url(url: str) -> str:
    """Validates ``--public-url``: HTTPS (or http on loopback for local tests), no query."""
    clean = (url or "").strip().rstrip("/")
    parsed = urlparse(clean)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("--public-url deve essere un URL completo, es. https://mio-pc.tail1234.ts.net")
    if parsed.scheme == "http" and not is_loopback(parsed.hostname):
        raise ValueError("--public-url deve usare HTTPS (Tailscale Funnel e Cloudflare Tunnel lo forniscono).")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("--public-url deve essere solo l'origine, senza percorso: il server aggiunge /mcp.")
    return clean


class BearerAuthMiddleware:
    """Pure ASGI middleware enforcing ``Authorization: Bearer <token>``.

    Implemented at the ASGI level (not ``BaseHTTPMiddleware``) so it never
    interferes with the SSE streaming used by the streamable-http transport.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if not hmac.compare_digest(provided, self._expected):
            await _json_error(
                send,
                401,
                b'{"error":"unauthorized","error_description":"Bearer token required"}',
                extra=[(b"www-authenticate", b'Bearer realm="classeviva-mcp"')],
            )
            return

        await self.app(scope, receive, send)


async def _json_error(send: Any, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(extra or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class HostAllowlistMiddleware:
    """Rejects any HTTP request whose ``Host`` is not loopback or the public host.

    The SDK checks ``Host`` only on the MCP endpoint; this extends the same
    DNS-rebinding protection to the OAuth and consent routes.
    """

    def __init__(self, app: Any, host: str, public_url: str | None) -> None:
        self.app = app
        names = {"127.0.0.1", "localhost", "[::1]", "::1"}
        if host and not is_loopback(host):
            names.add(host.lower())
        if public_url:
            names.add((urlparse(public_url).hostname or "").lower())
        self._names = names

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"host", b"").decode("latin-1").strip().lower()
            name = raw.rsplit(":", 1)[0] if raw and not raw.endswith("]") else raw
            if name not in self._names:
                await _json_error(send, 421, b'{"error":"misdirected_request"}')
                return
        await self.app(scope, receive, send)


def _transport_security(host: str, public_url: str | None) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if host and not is_loopback(host):
        hosts += [host, f"{host}:*"]
    if public_url:
        parsed = urlparse(public_url)
        netloc = parsed.netloc
        hosts += [netloc, f"{parsed.hostname}:*"] if parsed.port is None else [netloc]
        origins.append(f"{parsed.scheme}://{netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_app(
    mcp_server: Any,
    token: str | None = None,
    *,
    path: str = "/mcp",
    host: str = "127.0.0.1",
    public_url: str | None = None,
    oauth_provider: Any | None = None,
) -> Any:
    """Builds the ASGI app for the chosen auth mode (``oauth_provider`` or ``token``)."""
    security = _transport_security(host, public_url)
    if oauth_provider is None:
        if not token:
            raise ValueError("Serve un token o un provider OAuth.")
        app = mcp_server.streamable_http_app(streamable_http_path=path, host=host, transport_security=security)
        return HostAllowlistMiddleware(BearerAuthMiddleware(app, token), host, public_url)

    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from classeviva_mcp.oauth import SCOPE, consent_routes, metadata_patch_middleware

    if not public_url:
        raise ValueError("La modalità OAuth richiede --public-url.")
    auth = AuthSettings(
        # Strings (not AnyHttpUrl) keep the canonical issuer without a trailing
        # slash: RFC 8414 issuer comparison is an exact string match.
        issuer_url=public_url,  # type: ignore[arg-type]
        resource_server_url=f"{public_url}{path}",  # type: ignore[arg-type]
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=True,
    )
    # The public MCPServer API takes auth only at construction time, while the
    # same server object also serves stdio; build the HTTP app from the
    # low-level server with explicit auth arguments instead of mutating it.
    lowlevel = mcp_server._lowlevel_server  # noqa: SLF001 - stable within mcp<3 (pinned)
    app = lowlevel.streamable_http_app(
        streamable_http_path=path,
        transport_security=security,
        host=host,
        auth=auth,
        token_verifier=ProviderTokenVerifier(oauth_provider),
        auth_server_provider=oauth_provider,
        custom_starlette_routes=consent_routes(oauth_provider),
    )
    return HostAllowlistMiddleware(metadata_patch_middleware(app), host, public_url)


def run_http_server(
    mcp_server: Any,
    *,
    host: str,
    port: int,
    token: str | None = None,
    path: str = "/mcp",
    public_url: str | None = None,
    oauth_provider: Any | None = None,
) -> None:
    """Runs the MCP server over streamable HTTP behind the chosen auth gate."""
    import uvicorn

    app = build_app(
        mcp_server, token, path=path, host=host, public_url=public_url, oauth_provider=oauth_provider
    )
    mode = "OAuth" if oauth_provider is not None else "token bearer"
    print(f"Server MCP in ascolto su http://{host}:{port}{path} (autenticazione: {mode}).", file=sys.stderr)
    if public_url:
        print(f"URL del connettore da usare nell'app: {public_url}{path}", file=sys.stderr)
    # proxy_headers: trust X-Forwarded-* only from the local tunnel daemon.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1",
        server_header=False,
    )
