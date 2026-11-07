from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from classeviva_mcp.models import StudentContext, current_school_year_str, today_rome_str

SERVICE_NAME = "classeviva-mcp"
MAX_SEEN_NOTICES = 200
MAX_CUSTOM_NOTES_CHARS = 1500



def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    target = base / SERVICE_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


@dataclass
class StoredCredentials:
    username: str
    password: str
    classroom_ical_url: str = ""
    google_access_token: str = ""
    preferred_profile: str = ""
    source: str = "keyring"


class CredentialStore:
    """Manages credentials and persistent student context across Windows, macOS, and Linux/Termux:
    1. Environment variables (CLASSEVIVA_USER, CLASSEVIVA_PASSWORD)
    2. Native OS Keyring (Windows Credential Manager / macOS Keychain / Linux SecretService)
    3. Restricted 0600 file in cross-platform user data dir
    """

    def __init__(self, fallback_dir: Path | None = None) -> None:
        self.data_dir = fallback_dir or _user_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_file = self.data_dir / "credentials.json"
        self.session_file = self.data_dir / "session_cache.json"
        self.context_file = self.data_dir / "student_context.json"

    def save(
        self,
        username: str,
        password: str,
        classroom_ical_url: str = "",
        preferred_profile: str = "",
        google_access_token: str = "",
    ) -> str:
        """Saves credentials into OS Keyring (or fallback 0600 file if keyring unavailable).
        Returns the backend name used ('keyring' or 'file').
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
                if self.fallback_file.exists():
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
        self._write_private_json(self.fallback_file, payload)
        return "file"

    def save_classroom_ical_url(self, url: str) -> None:
        """Salva o aggiorna l'URL iCal di Classroom sia nel portachiavi sia nel contesto studente."""
        clean = url.strip()
        try:
            keyring.set_password(SERVICE_NAME, "classroom_ical_url", clean)
        except Exception:
            pass
        if self.fallback_file.exists():
            try:
                data = json.loads(self.fallback_file.read_text(encoding="utf-8"))
                data["classroom_ical_url"] = clean
                self._write_private_json(self.fallback_file, data)
            except Exception:
                pass
        ctx = self.load_student_context()
        ctx.classroom_ical_url = clean
        self.save_student_context(ctx)

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
            ctx_ical = self.load_student_context().classroom_ical_url
            kr_ical = env_ical or (keyring.get_password(SERVICE_NAME, "classroom_ical_url") or "") or ctx_ical
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
                data = json.loads(self.fallback_file.read_text(encoding="utf-8"))
                if data.get("username") and data.get("password"):
                    ctx_ical = self.load_student_context().classroom_ical_url
                    return StoredCredentials(
                        username=data["username"],
                        password=data["password"],
                        classroom_ical_url=env_ical or data.get("classroom_ical_url") or ctx_ical,
                        google_access_token=env_gtok or data.get("google_access_token", ""),
                        preferred_profile=env_prof or data.get("preferred_profile", ""),
                        source="file",
                    )
            except Exception:
                pass


        raise RuntimeError(
            "Credenziali ClasseViva non configurate. "
            "Esegui `classeviva-mcp login` nel terminale per salvarle nel gestore credenziali di sistema, "
            "oppure imposta CLASSEVIVA_USER e CLASSEVIVA_PASSWORD."
        )

    def clear(self) -> None:
        """Removes stored credentials and cached session tokens."""
        for key in ("username", "password", "classroom_ical_url", "preferred_profile", "google_access_token"):
            try:
                keyring.delete_password(SERVICE_NAME, key)
            except Exception:
                pass
        self.fallback_file.unlink(missing_ok=True)
        self.session_file.unlink(missing_ok=True)

    def save_session(self, session_data: dict[str, Any]) -> None:
        self._write_private_json(self.session_file, session_data)

    def load_session(self) -> dict[str, Any] | None:
        if not self.session_file.exists():
            return None
        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # --- Persistent Student Context (Class, Course, Register Number, Timetable) ---

    def load_student_context(self) -> StudentContext:
        if not self.context_file.exists():
            return StudentContext()
        try:
            raw = json.loads(self.context_file.read_text(encoding="utf-8"))
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


    @staticmethod
    def _write_private_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
