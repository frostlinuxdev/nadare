# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Make HTTPS work when the bundled OpenSSL cannot find the host's CA store.

The AppImage ships its own OpenSSL, built with Debian-style default paths
(``/usr/lib/ssl``). Fedora/RHEL keep their bundle under ``/etc/pki/tls``, so
every HTTPS fetch failed with ``CERTIFICATE_VERIFY_FAILED``. When the default
store is missing we point ``SSL_CERT_FILE`` at the first well-known bundle; an
explicit ``SSL_CERT_FILE``/``SSL_CERT_DIR`` from the user always wins.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Iterable

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


__all__ = ["CA_BUNDLES", "ensure_ca_bundle"]
