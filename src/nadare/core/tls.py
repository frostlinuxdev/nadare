# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pin Nadare's own HTTPS verification to the bundled certifi CA list.

curl_cffi already verifies against ``certifi``. Nadare's ``urllib`` /
``http.client`` paths now do the same, so integrity-critical downloads do not
depend on a lagging distro store. An explicit ``NADARE_CA_BUNDLE`` (a PEM
file), ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` from the user always wins, for
corporate proxies.

The old fallback remains for builds without ``certifi``: the AppImage ships its
own OpenSSL, built with Debian-style default paths (``/usr/lib/ssl``), while
Fedora/RHEL keep their bundle under ``/etc/pki/tls``. When no default store is
found we point ``SSL_CERT_FILE`` at the first well-known bundle.
"""

from __future__ import annotations

import os
import ssl
import urllib.request
from collections.abc import Callable, Iterable

#: Hosts that serve a certificate chain the public Web PKI should reject. A
#: distro image (or an enterprise proxy) that accepts one of these has a forged
#: root in its CA store, which would also let a MITM sign Nadare's index data.
DIAGNOSTIC_HOSTS: tuple[str, ...] = (
    "https://dv-g3b-emudhra.we-have-a-problem.com/",
    "https://dv-g3b-aaa-emudhra.we-have-a-problem.com/",
)


def _is_certificate_failure(exc: BaseException) -> bool:
    """True only for a certificate-verification failure, not any URLError."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        cur = pending.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if isinstance(cur, ssl.CertificateError):
            return True
        pending.extend(
            (
                getattr(cur, "reason", None),
                cur.__cause__,
                cur.__context__,
            )
        )
        pending.extend(arg for arg in cur.args if isinstance(arg, BaseException))
    text = str(exc).lower()
    return "certificate verify failed" in text or "certificate_verify_failed" in text


def check_forged_chain_hosts(
    opener: Callable[..., object] | None = None, timeout: float = 10
) -> list[tuple[str, bool | None]]:
    """Probe the diagnostic hosts through the host CA store.

    Returns ``(host, rejected)``: ``True`` when TLS rejected the forged chain
    (good), ``False`` when it connected (the system trusts it), and ``None``
    when the host could not be reached for a non-certificate reason.
    """
    open_ = opener or urllib.request.urlopen
    results: list[tuple[str, bool | None]] = []
    for host in DIAGNOSTIC_HOSTS:
        try:
            response = open_(host, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - classify, never escape the probe
            results.append((host, True if _is_certificate_failure(exc) else None))
            continue
        close = getattr(response, "close", None)
        if callable(close):
            close()
        results.append((host, False))
    return results


CA_BUNDLES: tuple[str, ...] = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL, Nobara, openSUSE (compat)
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora/RHEL extracted
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Alpine
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # Alpine, macOS-style
    "/etc/pki/tls/cert.pem",
)


def _default_store_present() -> bool:
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.isfile(paths.cafile):
        return True
    return bool(paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath))


def ensure_ca_bundle(candidates: Iterable[str] = CA_BUNDLES) -> str | None:
    """Set ``SSL_CERT_FILE`` to a host CA bundle when OpenSSL has none.

    Returns the file that was selected, or ``None`` when nothing needed (or
    could be) done.
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None
    if _default_store_present():
        return None
    for path in candidates:
        if os.path.isfile(path):
            os.environ["SSL_CERT_FILE"] = path
            return path
    return None


def setup() -> str | None:
    """Prepare HTTPS trust at startup.

    With ``certifi`` available Nadare verifies with it directly (see
    :func:`ssl_context`), so nothing is exported: an auto-exported host
    ``SSL_CERT_FILE`` would otherwise read as a *user* override and silently
    replace the pinned CA list (it also leaks into game processes). Only a build
    without ``certifi`` falls back to :func:`ensure_ca_bundle`.
    """
    if os.environ.get("NADARE_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or os.environ.get(
        "SSL_CERT_DIR"
    ):
        return None
    try:
        import certifi  # noqa: F401
    except Exception:  # pragma: no cover - certifi is a runtime dependency
        return ensure_ca_bundle()
    return None


__all__ = [
    "CA_BUNDLES",
    "DIAGNOSTIC_HOSTS",
    "ca_source",
    "check_forged_chain_hosts",
    "clear_context_cache",
    "ensure_ca_bundle",
    "setup",
    "ssl_context",
    "urlopen",
]


#: Contexts keyed by the CA-related environment at the time they were built.
_CONTEXT_CACHE: dict[tuple[str | None, str | None, str | None], ssl.SSLContext] = {}


def clear_context_cache() -> None:
    """Forget cached contexts (tests; or after changing CA environment)."""
    _CONTEXT_CACHE.clear()


def _env_signature() -> tuple[str | None, str | None, str | None]:
    return (
        os.environ.get("NADARE_CA_BUNDLE") or None,
        os.environ.get("SSL_CERT_FILE") or None,
        os.environ.get("SSL_CERT_DIR") or None,
    )


def _resolve_source() -> tuple[str, str | None]:
    """(``ca_source`` name, CA file or ``None``) for the current environment."""
    bundle = os.environ.get("NADARE_CA_BUNDLE")
    if bundle and os.path.isfile(bundle):
        return "NADARE_CA_BUNDLE", bundle
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return "environment", None
    try:
        import certifi
    except Exception:  # pragma: no cover - certifi is a runtime dependency
        pass
    else:
        where = getattr(certifi, "where", None)
        if callable(where):
            return "certifi", where()
    return "system", None


def ca_source() -> str:
    """Which CA list HTTPS uses: NADARE_CA_BUNDLE, environment, certifi or system."""
    return _resolve_source()[0]


def ssl_context() -> ssl.SSLContext:
    """A verified ``SSLContext`` for Nadare's own HTTPS requests.

    Precedence: ``NADARE_CA_BUNDLE`` (an existing PEM file), then an explicit
    ``SSL_CERT_FILE``/``SSL_CERT_DIR`` (OpenSSL honours those), then the pinned
    ``certifi`` bundle, then the system store (with :func:`ensure_ca_bundle`).
    The result is cached per environment signature.
    """
    signature = _env_signature()
    cached = _CONTEXT_CACHE.get(signature)
    if cached is not None:
        return cached

    source, cafile = _resolve_source()
    if source in ("NADARE_CA_BUNDLE", "certifi"):
        context = ssl.create_default_context(cafile=cafile)
    elif source == "environment":
        context = ssl.create_default_context()
    else:
        ensure_ca_bundle()
        context = ssl.create_default_context()
        # ensure_ca_bundle may have set SSL_CERT_FILE; cache under that
        # signature too so repeat calls reuse the same context.
        _CONTEXT_CACHE[_env_signature()] = context

    _CONTEXT_CACHE[signature] = context
    return context


def urlopen(
    url: str | urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext | None,
) -> object:
    """``urllib.request.urlopen`` with an explicit context.

    Wrappers that do not accept ``context`` (a few tests stub ``urlopen`` with
    an older signature) still work; the real function always does.
    """
    try:
        return urllib.request.urlopen(url, timeout=timeout, context=context)  # noqa: S310
    except TypeError as exc:
        if "context" not in str(exc):
            raise
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310

