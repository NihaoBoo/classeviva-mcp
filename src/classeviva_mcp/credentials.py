from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from classeviva_mcp.models import StudentContext, current_school_year_str, today_rome_str

SERVICE_NAME = "classeviva-mcp"
MAX_SEEN_NOTICES = 200
MAX_CUSTOM_NOTES_CHARS = 1500
DPAPI_MAGIC = b"DPAPI1\n"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    target = base / SERVICE_NAME
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi_crypt(data: bytes, *, decrypt: bool) -> bytes:
    """Encrypt/decrypt bytes with Windows DPAPI, bound to the current user profile."""
    if not _dpapi_available():
        raise RuntimeError("DPAPI è disponibile solo su Windows.")

    # ``ctypes.windll`` exists only on Windows; mypy runs this file on Linux too.
    windll = getattr(ctypes, "windll")  # noqa: B009 - attribute absent on non-Windows
    crypt32 = windll.crypt32
    kernel32 = windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    entropy_bytes = SERVICE_NAME.encode("utf-8")
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    blob_entropy = _DataBlob(
        len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char))
    )
    blob_out = _DataBlob()

    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(blob_entropy),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            "classeviva-mcp",
            ctypes.byref(blob_entropy),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
    if not ok:
        raise OSError("Operazione DPAPI fallita.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


@dataclass
class StoredCredentials:
    username: str
    password: str = field(repr=False)
    classroom_ical_url: str = field(default="", repr=False)
    google_access_token: str = field(default="", repr=False)
    preferred_profile: str = ""
    source: str = "keyring"


class CredentialStore:
    """Manages credentials and persistent student context across Windows, macOS, and Linux/Termux:
    1. Environment variables (CLASSEVIVA_USER, CLASSEVIVA_PASSWORD)
    2. Native OS Keyring (Windows Credential Manager / macOS Keychain / Linux SecretService)
    3. Encrypted fallback file (Windows DPAPI) or explicit opt-in plaintext file
    """

    def __init__(
        self,
        fallback_dir: Path | None = None,
        *,
        allow_insecure_plaintext: bool = False,
    ) -> None:
        self.data_dir = fallback_dir or _user_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allow_insecure_plaintext = allow_insecure_plaintext
        self._dpapi = _dpapi_available()
        self.fallback_file = self.data_dir / "credentials.json"
        self.session_file = self.data_dir / "session_cache.json"
        self.context_file = self.data_dir / "student_context.json"
        self.attachments_dir = self.data_dir / "attachments"
        self.mcp_token_file = self.data_dir / "mcp_token.json"
        self.oauth_state_file = self.data_dir / "oauth_state.json"

    # --- Credentials ---

    def probe_storage(self) -> str:
        """Checks that credentials can be stored securely, without persisting them.

        Returns the backend that would be used ('keyring', 'dpapi' or 'file').
        Raises RuntimeError when no secure storage is available.
        """
        probe_service = f"{SERVICE_NAME}-probe"
        try:
            keyring.set_password(probe_service, "probe", "1")
            if keyring.get_password(probe_service, "probe") == "1":
                try:
                    keyring.delete_password(probe_service, "probe")
                except Exception:
                    pass
                return "keyring"
        except Exception:
            pass
        try:
            keyring.delete_password(probe_service, "probe")
        except Exception:
            pass
        if self._dpapi:
            return "dpapi"
        if self.allow_insecure_plaintext:
            return "file"
        raise RuntimeError(
            "Keyring non disponibile e DPAPI non applicabile su questa piattaforma. "
            "Rifiuto di salvare credenziali in chiaro. Usa CLASSEVIVA_USER/CLASSEVIVA_PASSWORD "
            "oppure rilancia `login --allow-insecure-plaintext` se accetti il rischio."
        )

    def save(
        self,
        username: str,
        password: str,
        classroom_ical_url: str = "",
        preferred_profile: str = "",
        google_access_token: str = "",
    ) -> str:
        """Saves credentials into the OS Keyring or an encrypted fallback.
        Returns the backend used ('keyring', 'dpapi' or 'file').
        """
        user_clean = username.strip()
        ical_clean = classroom_ical_url.strip()
        prof_clean = preferred_profile.strip()
        gtok_clean = google_access_token.strip()

        try:
            keyring.set_password(SERVICE_NAME, "username", user_clean)
            keyring.set_password(SERVICE_NAME, "password", password)
            if ical_clean:
                keyring.set_password(SERVICE_NAME, "classroom_ical_url", ical_clean)
            if prof_clean:
                keyring.set_password(SERVICE_NAME, "preferred_profile", prof_clean)
            if gtok_clean:
                keyring.set_password(SERVICE_NAME, "google_access_token", gtok_clean)
            if keyring.get_password(SERVICE_NAME, "username") == user_clean:
                self.fallback_file.unlink(missing_ok=True)
                return "keyring"
        except (KeyringError, Exception):
            pass

        payload = {
            "username": user_clean,
            "password": password,
            "classroom_ical_url": ical_clean,
            "preferred_profile": prof_clean,
            "google_access_token": gtok_clean,
        }
        if self._dpapi:
            self._write_private_json(self.fallback_file, payload)
            return "dpapi"
        if self.allow_insecure_plaintext:
            self._write_private_json(self.fallback_file, payload)
            return "file"
        raise RuntimeError(
            "Keyring non disponibile e DPAPI non applicabile su questa piattaforma. "
            "Rifiuto di salvare credenziali in chiaro. Usa CLASSEVIVA_USER/CLASSEVIVA_PASSWORD "
            "oppure rilancia `login --allow-insecure-plaintext` se accetti il rischio."
        )

    def save_classroom_ical_url(self, url: str) -> None:
        """Salva o aggiorna l'URL iCal di Classroom nel Keyring o nel fallback cifrato."""
        clean = url.strip()
        try:
            keyring.set_password(SERVICE_NAME, "classroom_ical_url", clean)
            if self.fallback_file.exists():
                data = self._read_private_json(self.fallback_file)
                data["classroom_ical_url"] = clean
                self._write_private_json(self.fallback_file, data)
            return
        except Exception:
            pass

        if self._dpapi or self.allow_insecure_plaintext:
            data = self._read_private_json(self.fallback_file) if self.fallback_file.exists() else {}
            data["classroom_ical_url"] = clean
            self._write_private_json(self.fallback_file, data)
            return
        raise RuntimeError(
            "Impossibile salvare l'URL iCal: Keyring non disponibile e fallback cifrato non applicabile."
        )

    def has_classroom_ical_url(self) -> bool:
        if os.getenv("CLASSROOM_ICAL_URL", "").strip():
            return True
        try:
            if keyring.get_password(SERVICE_NAME, "classroom_ical_url"):
                return True
        except Exception:
            pass
        if self.fallback_file.exists():
            try:
                return bool(self._read_private_json(self.fallback_file).get("classroom_ical_url"))
            except Exception:
                return False
        return False

    def load(self) -> StoredCredentials:
        """Loads credentials following priority: env -> keyring -> fallback file."""
        env_user = os.getenv("CLASSEVIVA_USER", "").strip()
        env_pass = os.getenv("CLASSEVIVA_PASSWORD", "").strip()
        env_ical = os.getenv("CLASSROOM_ICAL_URL", "").strip()
        env_gtok = os.getenv("GOOGLE_ACCESS_TOKEN", "").strip()
        env_prof = os.getenv("CLASSEVIVA_PROFILE", "").strip()

        if env_user and env_pass:
            return StoredCredentials(
                username=env_user,
                password=env_pass,
                classroom_ical_url=env_ical,
                google_access_token=env_gtok,
                preferred_profile=env_prof,
                source="env",
            )

        try:
            kr_user = keyring.get_password(SERVICE_NAME, "username") or ""
            kr_pass = keyring.get_password(SERVICE_NAME, "password") or ""
            kr_ical = env_ical or (keyring.get_password(SERVICE_NAME, "classroom_ical_url") or "")
            kr_gtok = env_gtok or (keyring.get_password(SERVICE_NAME, "google_access_token") or "")
            kr_prof = env_prof or (keyring.get_password(SERVICE_NAME, "preferred_profile") or "")
            if kr_user and kr_pass:
                return StoredCredentials(
                    username=kr_user,
                    password=kr_pass,
                    classroom_ical_url=kr_ical,
                    google_access_token=kr_gtok,
                    preferred_profile=kr_prof,
                    source="keyring",
                )
        except (KeyringError, Exception):
            pass

        if self.fallback_file.exists():
            try:
                data = self._read_private_json(self.fallback_file)
                if data.get("username") and data.get("password"):
                    return StoredCredentials(
                        username=data["username"],
                        password=data["password"],
                        classroom_ical_url=env_ical or data.get("classroom_ical_url", ""),
                        google_access_token=env_gtok or data.get("google_access_token", ""),
                        preferred_profile=env_prof or data.get("preferred_profile", ""),
                        source="dpapi" if self._dpapi else "file",
                    )
            except Exception:
                pass

        raise RuntimeError(
            "Credenziali ClasseViva non configurate. "
            "Esegui `classeviva-mcp login` nel terminale per salvarle nel gestore credenziali di sistema, "
            "oppure imposta CLASSEVIVA_USER e CLASSEVIVA_PASSWORD."
        )

    def clear(self, purge_all: bool = False) -> None:
        """Removes stored credentials and cached session tokens.

        With ``purge_all=True`` also removes the student context and attachment cache.
        Raises RuntimeError if a Keyring entry could not be deleted.
        """
        errors: list[str] = []
        for key in (
            "username",
            "password",
            "classroom_ical_url",
            "preferred_profile",
            "google_access_token",
        ):
            try:
                existing = keyring.get_password(SERVICE_NAME, key)
                if existing is not None:
                    keyring.delete_password(SERVICE_NAME, key)
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}")

        self.fallback_file.unlink(missing_ok=True)
        self.session_file.unlink(missing_ok=True)
        if purge_all:
            self.context_file.unlink(missing_ok=True)
            shutil.rmtree(self.attachments_dir, ignore_errors=True)
        if errors:
            raise RuntimeError(
                "Alcune credenziali non sono state rimosse dal Keyring: " + ", ".join(errors)
            )

    # --- MCP HTTP token ---

    def save_mcp_token(self, token: str) -> Path:
        """Stores the bearer token used by the HTTP transport.

        DPAPI-encrypted on Windows, `0600` on Linux/macOS, same as the rest of
        the local state.
        """
        self._write_private_json(self.mcp_token_file, {"token": token})
        return self.mcp_token_file

    def load_mcp_token(self) -> str:
        """Reads the stored bearer token, or an empty string if there is none."""
        if not self.mcp_token_file.exists():
            return ""
        try:
            data = self._read_private_json(self.mcp_token_file)
        except Exception:
            return ""
        return str(data.get("token") or "")

    # --- Remote OAuth state (owner passphrase hash, clients, token hashes) ---

    def load_oauth_state(self) -> dict[str, Any]:
        if not self.oauth_state_file.exists():
            return {}
        return self._read_private_json(self.oauth_state_file)

    def save_oauth_state(self, state: dict[str, Any]) -> None:
        self._write_private_json(self.oauth_state_file, state)

    # --- Session cache ---

    def save_session(self, session_data: dict[str, Any]) -> None:
        if self._dpapi or self.allow_insecure_plaintext:
            self._write_private_json(self.session_file, session_data)

    def load_session(self) -> dict[str, Any] | None:
        if not self.session_file.exists():
            return None
        try:
            return self._read_private_json(self.session_file)
        except Exception:
            return None

    # --- Persistent Student Context (Class, Course, Register Number, Timetable) ---

    def load_student_context(self) -> StudentContext:
        if not self.context_file.exists():
            return StudentContext()
        try:
            raw = self._read_private_json(self.context_file)
            legacy_ical = str(raw.pop("classroom_ical_url", "") or "").strip()
            if legacy_ical:
                # Migrazione: il feed iCal non deve più vivere nel contesto in chiaro.
                try:
                    keyring.set_password(SERVICE_NAME, "classroom_ical_url", legacy_ical)
                except Exception:
                    pass
                self._write_private_json(self.context_file, raw)
            return StudentContext.model_validate(raw)
        except Exception:
            return StudentContext()

    def save_student_context(self, ctx: StudentContext) -> StudentContext:
        ctx.updated_at = today_rome_str()
        if not ctx.school_year:
            ctx.school_year = current_school_year_str()
        if len(ctx.custom_notes) > MAX_CUSTOM_NOTES_CHARS:
            ctx.custom_notes = ctx.custom_notes[:MAX_CUSTOM_NOTES_CHARS]
        if len(ctx.seen_notice_ids) > MAX_SEEN_NOTICES:
            ctx.seen_notice_ids = ctx.seen_notice_ids[-MAX_SEEN_NOTICES:]
        self._write_private_json(self.context_file, ctx.model_dump())
        return ctx

    def mark_notices_seen(self, notice_ids: list[str]) -> None:
        """Registra gli ID delle circolari già mostrate/lette mantenendo una coda FIFO limitata a 200 elementi."""
        if not notice_ids:
            return
        ctx = self.load_student_context()
        existing = list(ctx.seen_notice_ids)
        for nid in notice_ids:
            s_nid = str(nid).strip()
            if s_nid and s_nid not in existing:
                existing.append(s_nid)
        ctx.seen_notice_ids = existing[-MAX_SEEN_NOTICES:]
        self.save_student_context(ctx)

    # --- Private serialization helpers ---

    def _write_private_json(self, path: Path, data: dict[str, Any]) -> None:
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        if self._dpapi:
            raw = DPAPI_MAGIC + _dpapi_crypt(raw, decrypt=False)
        self._write_private_bytes(path, raw)

    def _read_private_json(self, path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        if raw.startswith(DPAPI_MAGIC):
            raw = _dpapi_crypt(raw[len(DPAPI_MAGIC) :], decrypt=True)
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _write_private_bytes(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
