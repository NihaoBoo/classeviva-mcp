"""Short-lived, single-use binary handoff to a trusted MCP client."""

from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass, field

import pymupdf
from mcp.types import ResourceLink

ATTACHMENT_TTL = 90
MAX_HANDOFF_BYTES = 8 * 1024 * 1024
_MAX_PENDING_BYTES = 24 * 1024 * 1024
_MAX_PENDING_FILES = 8
MIME_TYPES = {"pdf": "application/pdf", "png": "image/png", "file": "application/octet-stream"}


def safe_filename(name: str, fallback: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    clean = "".join(ch for ch in clean if ch.isprintable())
    return clean[:140].rstrip(" .") or fallback


def render_pdf_page(raw: bytes, page: int, dpi: int = 150) -> bytes:
    if not raw.startswith(b"%PDF") or not 1 <= page <= 50:
        raise ValueError("Indica una pagina PDF valida (da 1 a 50).")
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            if page > len(doc):
                raise ValueError
            pdf_page = doc[page - 1]
            edge = max(pdf_page.rect.width, pdf_page.rect.height)
            if not math.isfinite(edge) or edge <= 0:
                raise ValueError
            scale = min(dpi / 72, 1800 / edge)
            return pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    except (ValueError, RuntimeError):
        raise ValueError("Impossibile renderizzare la pagina PDF richiesta.") from None


@dataclass(frozen=True)
class _Attachment:
    kind: str
    generation: str
    expires: float
    data: bytes = field(repr=False)


class AttachmentHandoff:
    def __init__(self) -> None:
        self._items: dict[str, _Attachment] = {}

    def _drop_expired(self, generation: str, now: float) -> None:
        for key, item in tuple(self._items.items()):
            if now >= item.expires or generation != item.generation:
                del self._items[key]

    def offer(self, data: bytes, filename: str, generation: str) -> ResourceLink:
        if not data or len(data) > MAX_HANDOFF_BYTES:
            raise ValueError("File vuoto o oltre 8 MiB per l'invio. Per un file grande, offri il salvataggio locale.")
        kind = "pdf" if data.startswith(b"%PDF") else "png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "file"
        now = time.monotonic()
        self._drop_expired(generation, now)
        while (len(self._items) >= _MAX_PENDING_FILES
               or len(data) + sum(len(item.data) for item in self._items.values()) > _MAX_PENDING_BYTES):
            del self._items[next(iter(self._items))]
        key = secrets.token_urlsafe(24)
        self._items[key] = _Attachment(kind=kind, generation=generation, expires=now + ATTACHMENT_TTL, data=data)
        return ResourceLink(
            uri=f"classeviva://attachment/{kind}/{key}", name=safe_filename(filename, "allegato.bin"),
            mime_type=MIME_TYPES[kind], size=len(data),
        )

    def take(self, kind: str, key: str, generation: str) -> bytes:
        self._drop_expired(generation, time.monotonic())
        item = self._items.get(key)
        if item is None or item.kind != kind:
            raise ValueError("Allegato non disponibile o scaduto. Preparalo di nuovo.")
        del self._items[key]
        return item.data
