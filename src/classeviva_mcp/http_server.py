"""HTTP transport with bearer-token authentication for the ClasseViva MCP server.

The default transport is still stdio (the client spawns the server as a child
process and talks over pipes). This module adds an opt-in `streamable-http`
transport for remote deployments (VPS, phone, bot bridges).

Security model:

- A bearer token is **always required** for the HTTP transport.
- The token is compared in constant time.
- The server binds to loopback by default; binding to a routable address
  prints a loud warning because the traffic (and the token) travel in clear.
- The token is stored in the user data directory: DPAPI-encrypted on Windows,
  `0600` on Linux/macOS.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Any

TOKEN_ENV_VAR = "CLASSEVIVA_MCP_TOKEN"
TOKEN_FILENAME = "mcp_token"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})
TOKEN_BYTES = 32


def is_loopback(host: str) -> bool:
    """True when the host is a loopback address (only reachable from this PC)."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def resolve_token(token_file: str | None, data_dir: Path) -> tuple[str, str]:
    """Returns the bearer token to use and a human-readable source description.

    Order: explicit ``--token-file`` -> ``CLASSEVIVA_MCP_TOKEN`` -> a token
    generated once and stored in the data directory.
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

    generated = secrets.token_urlsafe(TOKEN_BYTES)
    return generated, "generato automaticamente"


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
            await self._reject(send)
            return

        await self.app(scope, receive, send)

    async def _reject(self, send: Any) -> None:
        body = b'{"error":"unauthorized","error_description":"Bearer token required"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="classeviva-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_app(mcp_server: Any, token: str, *, path: str = "/mcp", host: str = "127.0.0.1") -> Any:
    """Builds the Starlette app with the bearer-auth middleware in front."""
    app = mcp_server.streamable_http_app(streamable_http_path=path, host=host)
    return BearerAuthMiddleware(app, token)


def run_http_server(
    mcp_server: Any,
    *,
    host: str,
    port: int,
    token: str,
    path: str = "/mcp",
) -> None:
    """Runs the MCP server over streamable HTTP behind the bearer-token gate."""
    import uvicorn

    app = build_app(mcp_server, token, path=path, host=host)
    if not is_loopback(host):
        print(
            "ATTENZIONE: il server e' in ascolto su un indirizzo raggiungibile dalla rete.\n"
            "Il traffico HTTP NON e' cifrato: il token viaggia in chiaro.\n"
            "Usalo solo su una rete privata fidata o dietro un tunnel (es. Tailscale, SSH).",
            file=sys.stderr,
        )
    print(f"Server MCP in ascolto su http://{host}:{port}{path} (token richiesto).", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
