"""HTTP client factory shared by every outbound connection.

School and corporate networks are the most common reason a fresh install
"works at home but not at school":

* a mandatory **proxy**, configured in Windows (Internet Options) or via
  ``HTTPS_PROXY``. With ``trust_env=False`` httpx ignored it and every request
  failed with a connection error or a timeout;
* **TLS inspection**: the school firewall re-signs HTTPS traffic with its own
  root certificate, installed in the Windows certificate store by the IT
  department. httpx verifies against the ``certifi`` bundle, which does not
  contain that root, so every request failed with
  ``CERTIFICATE_VERIFY_FAILED``.

This module verifies certificates against the **operating system trust
store** (via ``truststore``, the same approach used by pip and uv's
``--native-tls``) and, by default, honours the system proxy. Both behaviours
can be turned off with environment variables:

* ``CLASSEVIVA_PROXY=off`` – ignore system/env proxies (direct connection).
* ``CLASSEVIVA_TLS=certifi`` – verify against the bundled ``certifi`` roots
  only, as in versions <= 1.2.x.

Certificate verification is never disabled.
"""

from __future__ import annotations

import logging
import os
import ssl
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FALSY = {"0", "off", "false", "no", "none", "direct"}


def proxy_enabled() -> bool:
    """True unless ``CLASSEVIVA_PROXY`` explicitly disables system proxies."""
    return os.getenv("CLASSEVIVA_PROXY", "auto").strip().lower() not in _FALSY


def tls_mode() -> str:
    """``system`` (OS trust store, default) or ``certifi``."""
    value = os.getenv("CLASSEVIVA_TLS", "system").strip().lower()
    return "certifi" if value == "certifi" else "system"


_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_BACKEND = "certifi"


def ssl_context() -> ssl.SSLContext | bool:
    """Returns the SSL context used for verification (never ``False``)."""
    global _SSL_CONTEXT, _SSL_BACKEND
    if tls_mode() == "certifi":
        _SSL_BACKEND = "certifi"
        return True
    if _SSL_CONTEXT is None:
        try:
            import truststore

            _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            _SSL_BACKEND = "system"
        except Exception as exc:  # noqa: BLE001 - optional accelerator, fall back safely
            logger.debug("truststore non disponibile (%s): uso certifi", type(exc).__name__)
            _SSL_BACKEND = "certifi"
            return True
    return _SSL_CONTEXT


def tls_backend_label() -> str:
    ssl_context()
    if _SSL_BACKEND == "system":
        return "certificati di sistema (compatibile con filtri HTTPS scolastici)"
    return "bundle certifi"


def detected_proxy() -> str:
    """Returns a redacted description of the proxy that will be used, if any."""
    if not proxy_enabled():
        return "disattivato (CLASSEVIVA_PROXY=off)"
    try:
        from urllib.request import getproxies

        proxies = getproxies()
    except Exception:  # noqa: BLE001 - diagnostics only
        return "non rilevabile"
    url = proxies.get("https") or proxies.get("all") or ""
    if not url:
        return "nessuno (connessione diretta)"
    try:
        parsed = httpx.URL(url if "://" in url else f"http://{url}")
        return f"{parsed.scheme}://{parsed.host}:{parsed.port or ''}".rstrip(":")
    except Exception:  # noqa: BLE001 - never echo credentials embedded in the URL
        return "configurato"


def build_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Creates an ``httpx.AsyncClient`` with the shared TLS/proxy policy.

    Callers keep full control over timeouts, redirects and limits; only
    ``verify`` and ``trust_env`` are defaulted here.
    """
    kwargs.setdefault("verify", ssl_context())
    kwargs.setdefault("trust_env", proxy_enabled())
    return httpx.AsyncClient(**kwargs)


def describe_network_error(exc: BaseException) -> str:
    """Turns a low-level httpx error into an actionable Italian message."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "certificate" in lowered or "ssl" in lowered:
        return (
            "Errore certificato TLS verso Spaggiari. Sulle reti scolastiche succede "
            "quando il filtro web ispeziona l'HTTPS. Il server usa già i certificati "
            "di sistema: se l'errore persiste, la rete blocca ClasseViva, prova da "
            "un'altra rete (es. hotspot)."
        )
    if isinstance(exc, httpx.ProxyError) or "proxy" in lowered:
        return (
            "Errore del proxy di rete. Controlla le impostazioni proxy di sistema "
            "oppure imposta CLASSEVIVA_PROXY=off per una connessione diretta."
        )
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Timeout verso web.spaggiari.eu: la rete è lenta o filtra il sito. "
            "Su un PC scolastico potrebbe servire il proxy della scuola."
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            "Impossibile raggiungere web.spaggiari.eu (rete assente, DNS o firewall). "
            "Su un PC scolastico controlla che il browser apra web.spaggiari.eu."
        )
    return f"Errore di rete verso Spaggiari ({type(exc).__name__})."
