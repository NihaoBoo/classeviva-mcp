"""Fresh-install robustness: stable MCP config, login errors, school networks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from classeviva_mcp import __main__ as cli
from classeviva_mcp import network
from classeviva_mcp.client import ClasseVivaAuthError, ClasseVivaError, SpaggiariClient
from classeviva_mcp.credentials import CredentialStore, StoredCredentials

# --- MCP config generation ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\s03527\AppData\Local\uv\cache\archive-v0\uL2a2w413jRVNm0B\Scripts\classeviva-mcp.EXE",
        "/home/me/.cache/uv/archive-v0/abc123/bin/classeviva-mcp",
        "/Users/me/.cache/uv/builds-v0/.tmpXYZ/bin/python",
    ],
)
def test_uv_cache_paths_are_ephemeral(path: str) -> None:
    # PureWindowsPath parts work on any OS; emulate by splitting manually.
    candidate = Path(path.replace("\\", "/"))
    assert cli._looks_ephemeral(candidate)


@pytest.mark.parametrize(
    "path",
    [
        "/home/me/.local/bin/classeviva-mcp",
        "/home/me/.local/share/uv/tools/classeviva-mcp/bin/classeviva-mcp",
        "C:/Users/me/.local/bin/classeviva-mcp.exe",
    ],
)
def test_stable_paths_are_not_ephemeral(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    assert not cli._looks_ephemeral(Path(path))


def test_uvx_run_never_points_config_to_uv_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression: `uvx classeviva-mcp login` printed the cache exe path."""
    cache_exe = tmp_path / "uv" / "cache" / "archive-v0" / "HASH" / "Scripts" / "classeviva-mcp.EXE"
    cache_exe.parent.mkdir(parents=True)
    cache_exe.write_text("")
    uvx = tmp_path / "bin" / "uvx"
    uvx.parent.mkdir()
    uvx.write_text("")

    monkeypatch.setattr(cli, "__file__", str(cache_exe.parent.parent / "Lib" / "site-packages" / "x" / "m.py"))
    monkeypatch.setattr(cli.sys, "prefix", str(cache_exe.parent.parent))
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: {"classeviva-mcp": str(cache_exe), "uvx": str(uvx)}.get(name),
    )

    command, args, kind = cli._resolve_launch_command()
    assert kind == "uvx"
    assert command == str(uvx)
    assert args == ["classeviva-mcp"]
    generic = json.loads(cli._build_ready_mcp_config())
    assert "cache" not in generic["mcpServers"]["classeviva"]["command"]


def test_installed_tool_uses_stable_shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shim = tmp_path / ".local" / "bin" / "classeviva-mcp"
    shim.parent.mkdir(parents=True)
    shim.write_text("")
    tool_env = tmp_path / ".local" / "share" / "uv" / "tools" / "classeviva-mcp"
    monkeypatch.setattr(cli, "__file__", str(tool_env / "lib" / "site-packages" / "classeviva_mcp" / "__main__.py"))
    monkeypatch.setattr(cli.sys, "prefix", str(tool_env))
    monkeypatch.setattr(cli.sys, "executable", str(tool_env / "bin" / "python"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(shim) if name == "classeviva-mcp" else None)

    command, args, kind = cli._resolve_launch_command()
    assert (command, args, kind) == (str(shim), [], "installed")


@pytest.mark.parametrize("client", cli.CLIENT_FORMATS)
def test_every_client_format_renders(client: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_resolve_launch_command", lambda: (r"C:\u\uvx.exe", ["classeviva-mcp"], "uvx"))
    out = cli._build_ready_mcp_config(client)
    if client == "opencode":
        data = json.loads(out)
        assert data["mcp"]["classeviva"] == {
            "type": "local",
            "command": [r"C:\u\uvx.exe", "classeviva-mcp"],
            "enabled": True,
        }
    elif client == "vscode":
        assert json.loads(out)["servers"]["classeviva"]["type"] == "stdio"
    elif client == "generic":
        assert json.loads(out)["mcpServers"]["classeviva"]["args"] == ["classeviva-mcp"]
    elif client == "codex":
        assert out.startswith("[mcp_servers.classeviva]")
        assert '"C:\\\\u\\\\uvx.exe"' in out  # backslashes escaped for TOML
    else:
        assert out.startswith("claude mcp add")


# --- Login error handling ----------------------------------------------------


def _client_with(handler: Any, username: str = "S1234567X") -> SpaggiariClient:
    creds = StoredCredentials(username=username, password="pw", source="login")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SpaggiariClient(http_client=http, credentials=creds, persist_session=False)


@pytest.mark.asyncio
async def test_rejected_credentials_give_actionable_message_and_cooldown(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"statusCode": 422, "error": "251/CvvRestApi/wrong credentials"})

    client = _client_with(handler, username="mario@example.com")
    client.store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    with pytest.raises(ClasseVivaAuthError) as info:
        await client.login()
    msg = str(info.value)
    assert "251" in msg
    assert "codice utente" in msg  # email hint
    assert "15-30 minuti" in msg

    # A burst of tool calls must not hammer Spaggiari with the same bad password.
    for _ in range(3):
        with pytest.raises(ClasseVivaAuthError, match="sospeso"):
            await client.login()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_html_block_page_is_reported_as_network_filter(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Sito bloccato</html>", headers={"content-type": "text/html"})

    client = _client_with(handler)
    with pytest.raises(ClasseVivaError, match="filtro web"):
        await client.login()


@pytest.mark.asyncio
async def test_connection_errors_are_explained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in chain")

    client = _client_with(handler)
    with pytest.raises(ClasseVivaError, match="certificato TLS") as info:
        await client.login()
    assert not isinstance(info.value, ClasseVivaAuthError)


@pytest.mark.asyncio
async def test_email_with_multiple_profiles_picks_student() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if not body.get("ident"):
            return httpx.Response(
                200, json={"requestedAction": "ChoiceIdent", "choices": [{"ident": "S7654321Y"}, {"ident": "X1"}]}
            )
        return httpx.Response(200, json={"token": "t", "ident": "S7654321Y"})

    client = _client_with(handler, username="mario@example.com")
    profile = await client.login()
    assert profile.student_id == "7654321"
    assert seen[-1]["ident"] == "S7654321Y"


# --- Network policy ----------------------------------------------------------


def test_network_defaults_use_system_trust_and_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLASSEVIVA_PROXY", raising=False)
    monkeypatch.delenv("CLASSEVIVA_TLS", raising=False)
    assert network.proxy_enabled()
    assert network.tls_mode() == "system"


def test_network_opt_outs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLASSEVIVA_PROXY", "off")
    monkeypatch.setenv("CLASSEVIVA_TLS", "certifi")
    assert not network.proxy_enabled()
    assert network.ssl_context() is True  # verification stays on, bundled roots
    assert "disattivato" in network.detected_proxy()


def test_password_control_chars_detected() -> None:
    assert cli._has_control_chars("abc\x16")
    assert not cli._has_control_chars("pass word!")
