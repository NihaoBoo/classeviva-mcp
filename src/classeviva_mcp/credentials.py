from __future__ import annotations

import ctypes
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import NoKeyringError

from classeviva_mcp.models import StudentContext, current_school_year_str, today_rome_str

SERVICE_NAME = "classeviva-mcp"
_ACCOUNT_KEY = "spaggiari_account"
_LEGACY_ACCOUNT_KEYS = ("username", "password", "preferred_profile")
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
        self.didactics_dir = self.data_dir / "didactics"
        self.auth_generation_file = self.data_dir / "auth_generation"
        self.classroom_generation_file = self.data_dir / "classroom_generation"
        self.classroom_grant_file = self.data_dir / "classroom_oauth.json"
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
        account = {"username": username.strip(), "password": password, "preferred_profile": preferred_profile.strip()}
        previous = self._read_private_json(self.fallback_file) if self.fallback_file.exists() else {}
        legacy_google = {key: previous[key] for key in ("classroom_ical_url", "google_access_token") if previous.get(key)}
        if classroom_ical_url.strip():
            legacy_google["classroom_ical_url"] = classroom_ical_url.strip()
        if google_access_token.strip():
            legacy_google["google_access_token"] = google_access_token.strip()
        serialized = json.dumps(account)
        try:
            keyring.set_password(SERVICE_NAME, _ACCOUNT_KEY, serialized)
            in_keyring = keyring.get_password(SERVICE_NAME, _ACCOUNT_KEY) == serialized
        except Exception:
            in_keyring = False

        if in_keyring:
            for key, value in legacy_google.items():
                keyring.set_password(SERVICE_NAME, key, value)
            self.fallback_file.unlink(missing_ok=True)
            self._invalidate_session()
            if self._clear_keyring(_LEGACY_ACCOUNT_KEYS):
                raise RuntimeError("Aggiornamento incompleto: rimuovi le credenziali legacy con `logout`.")
            return "keyring"
        if self._dpapi or self.allow_insecure_plaintext:
            self._write_private_json(self.fallback_file, {**account, **legacy_google})
            self._invalidate_session()
            return "dpapi" if self._dpapi else "file"
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
            self._bump_generation(self.classroom_generation_file)
            return
        except Exception:
            pass

        if self._dpapi or self.allow_insecure_plaintext:
            data = self._read_private_json(self.fallback_file) if self.fallback_file.exists() else {}
            data["classroom_ical_url"] = clean
            self._write_private_json(self.fallback_file, data)
            self._bump_generation(self.classroom_generation_file)
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

    def load_classroom_sources(self) -> tuple[str, str]:
        """Read legacy Google sources independently from the Spaggiari account."""
        ical = os.getenv("CLASSROOM_ICAL_URL", "").strip()
        token = os.getenv("GOOGLE_ACCESS_TOKEN", "").strip()
        try:
            ical = ical or keyring.get_password(SERVICE_NAME, "classroom_ical_url") or ""
            token = token or keyring.get_password(SERVICE_NAME, "google_access_token") or ""
        except NoKeyringError:
            pass
        except Exception:
            if not self.fallback_file.exists():
                raise
        if self.fallback_file.exists() and not (ical and token):
            data = self._read_private_json(self.fallback_file)
            ical = ical or str(data.get("classroom_ical_url") or "")
            token = token or str(data.get("google_access_token") or "")
        return ical, token

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

        account: dict[str, Any] = {}
        source = "keyring"
        if self.fallback_file.exists():
            try:
                data = self._read_private_json(self.fallback_file)
                if data.get("username") and data.get("password"):
                    account = data
                    source = "dpapi" if self._dpapi else "file"
            except Exception:
                pass

        if not account:
            try:
                raw = keyring.get_password(SERVICE_NAME, _ACCOUNT_KEY)
                account = json.loads(raw) if raw else {
                    key: keyring.get_password(SERVICE_NAME, key) for key in _LEGACY_ACCOUNT_KEYS
                }
            except Exception:
                account = {}
        if isinstance(account, dict) and account.get("username") and account.get("password"):
            try:
                env_ical, env_gtok = self.load_classroom_sources()
            except Exception:
                pass  # Google sources must not prevent a Spaggiari login.
            return StoredCredentials(
                username=account["username"], password=account["password"],
                classroom_ical_url=env_ical, google_access_token=env_gtok,
                preferred_profile=env_prof or account.get("preferred_profile") or "", source=source,
            )
        raise RuntimeError(
            "Credenziali ClasseViva non configurate. "
            "Esegui `classeviva-mcp login` nel terminale per salvarle nel gestore credenziali di sistema, "
            "oppure imposta CLASSEVIVA_USER e CLASSEVIVA_PASSWORD."
        )

    def auth_generation(self) -> str:
        return self._generation(self.auth_generation_file)

    def classroom_generation(self) -> str:
        return self._generation(self.classroom_generation_file)

    @staticmethod
    def _generation(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii")
        except FileNotFoundError:
            return ""

    def _bump_generation(self, path: Path) -> None:
        self._write_private_bytes(path, secrets.token_hex(16).encode("ascii"))

    def _invalidate_session(self) -> None:
        self._bump_generation(self.auth_generation_file)
        self.session_file.unlink(missing_ok=True)

    def save_classroom_grant(self, data: dict[str, Any]) -> None:
        """Google grants never use the legacy plaintext opt-in."""
        try:
            serialized = json.dumps(data)
            keyring.set_password(SERVICE_NAME, "classroom_oauth", serialized)
            if keyring.get_password(SERVICE_NAME, "classroom_oauth") != serialized:
                raise RuntimeError
        except Exception:
            if not self._dpapi:
                raise RuntimeError("Archivio sicuro Google non disponibile.") from None
            self._write_private_json(self.classroom_grant_file, data)
        else:
            self.classroom_grant_file.unlink(missing_ok=True)
        self._bump_generation(self.classroom_generation_file)

    def load_classroom_grant(self) -> dict[str, Any]:
        if self.classroom_grant_file.exists():
            if not self._dpapi or not self.classroom_grant_file.read_bytes().startswith(DPAPI_MAGIC):
                raise RuntimeError("Archivio Google non cifrato.")
            return self._read_private_json(self.classroom_grant_file)
        try:
            raw = keyring.get_password(SERVICE_NAME, "classroom_oauth")
        except NoKeyringError:
            return {}
        return json.loads(raw) if raw else {}

    def clear_classroom(self) -> None:
        """Clear Google OAuth and legacy Classroom sources, leaving ClasseViva connected."""
        errors = self._clear_keyring(("classroom_oauth", "classroom_ical_url", "google_access_token"))
        self.classroom_grant_file.unlink(missing_ok=True)
        if self.fallback_file.exists():
            data = self._read_private_json(self.fallback_file)
            data.pop("classroom_ical_url", None)
            data.pop("google_access_token", None)
            if data:
                self._write_private_json(self.fallback_file, data)
            else:
                self.fallback_file.unlink()
        self._clear_legacy_context_url()
        self._bump_generation(self.classroom_generation_file)
        if errors:
            raise RuntimeError("Rimozione delle credenziali Google dal Keyring non riuscita.")

    def _clear_legacy_context_url(self) -> None:
        if self.context_file.exists():
            data = self._read_private_json(self.context_file)
            if "classroom_ical_url" in data:
                del data["classroom_ical_url"]
                self._write_private_json(self.context_file, data)

    @staticmethod
    def _clear_keyring(keys: tuple[str, ...]) -> list[str]:
        errors = []
        for key in keys:
            try:
                if keyring.get_password(SERVICE_NAME, key) is not None:
                    keyring.delete_password(SERVICE_NAME, key)
            except NoKeyringError:
                # A machine using only DPAPI or the legacy file needs no Keyring.
                continue
            except Exception:
                errors.append(key)
        return errors

    def clear(self, purge_all: bool = False) -> None:
        """Removes stored credentials and cached session tokens.

        With ``purge_all=True`` also removes the student context and attachment cache.
        Raises RuntimeError if a Keyring entry could not be deleted.
        """
        errors = self._clear_keyring((
            _ACCOUNT_KEY,
            *_LEGACY_ACCOUNT_KEYS,
            "classroom_ical_url",
            "google_access_token",
            "classroom_oauth",
        ))
        try:
            self._invalidate_session()
        except OSError:
            errors.append("sessione")
        try:
            self._bump_generation(self.classroom_generation_file)
        except OSError:
            errors.append("sessione Google")
        files = [self.fallback_file, self.session_file, self.classroom_grant_file, self.mcp_token_file]
        if purge_all:
            files.extend((self.context_file, self.oauth_state_file))
        else:
            try:
                self._clear_legacy_context_url()
            except (OSError, ValueError):
                errors.append("iCal legacy")
            if self.oauth_state_file.exists():
                try:
                    owner = self.load_oauth_state().get("owner")
                    self.save_oauth_state({"owner": owner} if owner else {})
                except (OSError, ValueError, AttributeError):
                    errors.append("accesso remoto")
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                errors.append(path.name)
        for folder in (self.attachments_dir, self.didactics_dir) if purge_all else ():
            try:
                if folder.is_symlink():
                    folder.unlink()
                else:
                    shutil.rmtree(folder)
            except FileNotFoundError:
                pass
            except OSError:
                errors.append(folder.name)
        if errors:
            raise RuntimeError(
                "Rimozione locale incompleta: " + ", ".join(errors)
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
