from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import keyring
import pytest

from classeviva_mcp.client import SpaggiariClient
from classeviva_mcp.credentials import (
    DPAPI_MAGIC,
    SERVICE_NAME,
    CredentialStore,
)
from classeviva_mcp.models import StudentContext


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory replacement for the OS keyring, so tests never touch the user's vault."""
    vault: dict[tuple[str, str], str] = {}

    def _set(service: str, key: str, value: str) -> None:
        vault[(service, key)] = value

    def _get(service: str, key: str) -> str | None:
        return vault.get((service, key))

    def _delete(service: str, key: str) -> None:
        vault.pop((service, key), None)

    monkeypatch.setattr(keyring, "set_password", _set)
    monkeypatch.setattr(keyring, "get_password", _get)
    monkeypatch.setattr(keyring, "delete_password", _delete)
    return vault


def test_keyring_roundtrip_never_leaks_password(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str]
) -> None:
    store = CredentialStore(fallback_dir=tmp_path)
    backend = store.save(
        username="studente@example.com",
        password="SuperSegreta!",
        classroom_ical_url="https://calendar.google.com/calendar/ical/test/basic.ics",
    )
    assert backend == "keyring"
    assert all(key != "password" for _, key in fake_keyring)

    creds = store.load()
    assert creds.username == "studente@example.com"
    assert creds.password == "SuperSegreta!"
    assert creds.classroom_ical_url.startswith("https://calendar.google.com/")
    assert "SuperSegreta!" not in repr(creds)
    assert not store.fallback_file.exists()


def test_login_replaces_legacy_account_as_one_record_without_losing_ical(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CLASSEVIVA_USER", "CLASSEVIVA_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    fake_keyring[(SERVICE_NAME, "username")] = "S1"
    fake_keyring[(SERVICE_NAME, "password")] = "old"
    ical = "https://calendar.google.com/calendar/ical/synthetic/basic.ics"
    store._write_private_json(store.fallback_file, {"username": "S1", "password": "old", "classroom_ical_url": ical})
    store.save(username="S1", password="new")
    assert (SERVICE_NAME, "username") not in fake_keyring
    assert (SERVICE_NAME, "password") not in fake_keyring
    assert not store.fallback_file.exists()
    assert (store.load().username, store.load().password) == ("S1", "new")
    assert store.load_classroom_sources()[0] == ical


def test_save_fails_closed_without_keyring_or_dpapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", _boom)
    monkeypatch.setattr(keyring, "get_password", _boom)
    store = CredentialStore(fallback_dir=tmp_path)
    store._dpapi = False

    with pytest.raises(RuntimeError, match="Rifiuto di salvare credenziali in chiaro"):
        store.save(username="studente", password="pw")
    assert not store.fallback_file.exists()


def test_probe_storage_detects_keyring_without_persisting_credentials(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str]
) -> None:
    store = CredentialStore(fallback_dir=tmp_path)

    assert store.probe_storage() == "keyring"
    # The probe must not leave any credential behind.
    assert (SERVICE_NAME, "username") not in fake_keyring
    assert (SERVICE_NAME, "password") not in fake_keyring
    assert all(service != SERVICE_NAME for service, _ in fake_keyring)
    assert not store.fallback_file.exists()


def test_probe_storage_fails_closed_without_keyring_or_dpapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", _boom)
    monkeypatch.setattr(keyring, "get_password", _boom)
    monkeypatch.setattr(keyring, "delete_password", _boom)
    store = CredentialStore(fallback_dir=tmp_path)
    store._dpapi = False

    with pytest.raises(RuntimeError, match="Rifiuto di salvare credenziali in chiaro"):
        store.probe_storage()
    assert not store.fallback_file.exists()


def test_probe_storage_reports_plaintext_fallback_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", _boom)
    monkeypatch.setattr(keyring, "get_password", _boom)
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False

    assert store.probe_storage() == "file"


def test_plaintext_fallback_requires_explicit_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", _boom)
    monkeypatch.setattr(keyring, "get_password", _boom)
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False

    backend = store.save(username="studente", password="pw")
    assert backend == "file"
    assert store.fallback_file.exists()
    assert store.load().password == "pw"


def test_session_cache_roundtrip(tmp_path: Path) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False
    store.save_session({"token": "tok", "ident": "S1234567X", "student_id": "1234567"})
    assert store.load_session() == {
        "token": "tok",
        "ident": "S1234567X",
        "student_id": "1234567",
    }


def test_clear_keeps_context_unless_purge(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str]
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False
    store.save(username="studente", password="pw")
    store.save_session({"token": "tok", "student_id": "1"})
    store.save_student_context(StudentContext(school_class="5AI", custom_notes="note"))
    store.save_mcp_token("synthetic-mcp")
    store.save_oauth_state({"owner": {"hash": "synthetic"}, "grants": {"id": {}}, "clients": {"id": {}}})
    store.attachments_dir.mkdir(parents=True, exist_ok=True)
    (store.attachments_dir / "circolare.pdf").write_bytes(b"%PDF-1.4")

    store.clear()
    assert store.load_student_context().school_class == "5AI"
    assert not store.session_file.exists()
    assert not store.fallback_file.exists()
    assert not store.mcp_token_file.exists()
    assert store.load_oauth_state() == {"owner": {"hash": "synthetic"}}

    store.save(username="studente", password="pw")
    store.save_student_context(StudentContext(school_class="5AI"))
    store.attachments_dir.mkdir(parents=True, exist_ok=True)
    (store.attachments_dir / "circolare.pdf").write_bytes(b"%PDF-1.4")
    didactics = tmp_path / "didactics"
    didactics.mkdir()
    (didactics / "dispensa.pdf").write_bytes(b"%PDF-1.4")
    exported = tmp_path / "esportazione.pdf"
    exported.write_bytes(b"%PDF-1.4")
    store.clear(purge_all=True)
    assert store.load_student_context().school_class == ""
    assert not store.context_file.exists()
    assert not store.oauth_state_file.exists()
    assert not store.attachments_dir.exists()
    assert not didactics.exists()
    assert exported.exists()


@pytest.mark.asyncio
async def test_relogin_and_logout_invalidate_existing_client_and_disk_session(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    for name in ("CLASSEVIVA_USER", "CLASSEVIVA_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            uid = json.loads(request.content)["uid"]
            calls.append(uid)
            return httpx.Response(200, json={"token": f"token-{uid}", "ident": uid})
        return httpx.Response(200, json={"card": {"classDesc": "1A"}})

    store.save(username="S1", password="first")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpaggiariClient(store=store, http_client=http)
        assert (await client.get_profile()).ident == "S1"
        assert store.session_file.exists()
        store.save(username="S2", password="second")
        assert not store.session_file.exists()
        assert (await client.get_profile()).ident == "S2"
        store.clear()
        with pytest.raises(RuntimeError, match="non configurate"):
            await client.get_profile()
    assert calls == ["S1", "S2"]


def test_legacy_ical_url_is_migrated_to_keyring(
    tmp_path: Path, fake_keyring: dict[tuple[str, str], str]
) -> None:
    store = CredentialStore(fallback_dir=tmp_path, allow_insecure_plaintext=True)
    store._dpapi = False
    legacy_url = "https://calendar.google.com/calendar/ical/legacy/basic.ics"
    store._write_private_json(
        store.context_file,
        {"school_class": "5AI", "classroom_ical_url": legacy_url},
    )

    ctx = store.load_student_context()
    assert ctx.school_class == "5AI"
    assert not hasattr(ctx, "classroom_ical_url")
    assert fake_keyring[(SERVICE_NAME, "classroom_ical_url")] == legacy_url

    raw = store._read_private_json(store.context_file)
    assert "classroom_ical_url" not in raw


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI è disponibile solo su Windows")
def test_dpapi_fallback_is_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", _boom)
    monkeypatch.setattr(keyring, "get_password", _boom)
    store = CredentialStore(fallback_dir=tmp_path)
    assert store._dpapi is True

    backend = store.save(username="studente", password="pw")
    assert backend == "dpapi"
    assert store.fallback_file.read_bytes().startswith(DPAPI_MAGIC)
    assert store.load().password == "pw"
