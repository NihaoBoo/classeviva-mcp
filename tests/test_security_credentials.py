from __future__ import annotations

import sys
from pathlib import Path

import keyring
import pytest

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
    assert fake_keyring[(SERVICE_NAME, "password")] == "SuperSegreta!"

    creds = store.load()
    assert creds.username == "studente@example.com"
    assert creds.password == "SuperSegreta!"
    assert creds.classroom_ical_url.startswith("https://calendar.google.com/")
    assert "SuperSegreta!" not in repr(creds)
    assert not store.fallback_file.exists()


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
    store.attachments_dir.mkdir(parents=True, exist_ok=True)
    (store.attachments_dir / "circolare.pdf").write_bytes(b"%PDF-1.4")

    store.clear()
    assert store.load_student_context().school_class == "5AI"
    assert not store.session_file.exists()
    assert not store.fallback_file.exists()

    store.save(username="studente", password="pw")
    store.save_student_context(StudentContext(school_class="5AI"))
    store.attachments_dir.mkdir(parents=True, exist_ok=True)
    (store.attachments_dir / "circolare.pdf").write_bytes(b"%PDF-1.4")
    store.clear(purge_all=True)
    assert store.load_student_context().school_class == ""
    assert not store.context_file.exists()
    assert not store.attachments_dir.exists()


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
