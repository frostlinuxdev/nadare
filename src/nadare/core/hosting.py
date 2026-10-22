# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where Nadare's own published data lives: the manifest index and releases.

Everything that points at Nadare's hosting goes through here, so moving the
project is a one-file change.
Each URL can be overridden from the environment for testing and mirrors.

Trust note: the default index/release host serves manifests that are only
protected by TLS. Asset hashes are carried *inside* those manifests, so
they travel the same channel as the payloads; integrity therefore rests entirely
on the TLS trust in that hostname. Manifest signing (a pinned public key verified
independently of the fetched content) is the intended hardening and is tracked
separately; until then, treat overriding these URLs as trusting the new host.
"""

from __future__ import annotations

import os

#: Raw-file base of the public index repository (trailing slash required).
DEFAULT_INDEX_BASE = "https://raw.githubusercontent.com/frostlinuxdev/nadare-index/main/"

#: "Latest release" endpoint. Gitea/Forgejo (``/api/v1/repos/<o>/<r>/releases/latest``)
#: and GitHub (``https://api.github.com/repos/<o>/<r>/releases/latest``) return the
#: same fields Nadare reads: ``tag_name``, ``html_url``, ``name``.
DEFAULT_RELEASES_API = "https://api.github.com/repos/frostlinuxdev/nadare/releases/latest"

#: Pinned umu-launcher zipapp (the managed copy used when no system umu-run exists).
DEFAULT_UMU_ZIPAPP_URL = (
    "https://github.com/Open-Wine-Components/umu-launcher/releases/download/"
    "1.4.4/umu-launcher-1.4.4-zipapp.tar"
)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def index_base() -> str:
    """Base URL of the index repo, always ending in ``/`` (``""`` = disabled)."""
    base = _env("NADARE_INDEX_BASE", DEFAULT_INDEX_BASE)
    if base and not base.endswith("/"):
        base += "/"
    return base


def yostar_index_url() -> str:
    """Default Yostar manifest index (``""`` when the index is disabled)."""
    base = index_base()
    return f"{base}yostar/index.json" if base else ""


def czn_index_url() -> str:
    """Default CZN latest-version pointer (``""`` when the index is disabled)."""
    base = index_base()
    return f"{base}czn/latest.json" if base else ""


def webview2_index_url() -> str:
    """Published WebView2 installer hashes (``""`` when the index is disabled)."""
    base = index_base()
    return f"{base}webview2/latest.json" if base else ""


def vcredist_index_url() -> str:
    """Published Visual C++ redistributable hashes (``""`` when disabled)."""
    base = index_base()
    return f"{base}vcredist/latest.json" if base else ""


def releases_api() -> str:
    """The latest-release endpoint (``""`` disables the update check)."""
    return _env("NADARE_RELEASES_API", DEFAULT_RELEASES_API)


def umu_zipapp_url() -> str:
    """The pinned umu-launcher zipapp tarball (``""`` disables managed umu)."""
    return _env("NADARE_UMU_URL", DEFAULT_UMU_ZIPAPP_URL)


__all__ = [
    "DEFAULT_INDEX_BASE",
    "DEFAULT_RELEASES_API",
    "DEFAULT_UMU_ZIPAPP_URL",
    "czn_index_url",
    "index_base",
    "releases_api",
    "umu_zipapp_url",
    "vcredist_index_url",
    "webview2_index_url",
    "yostar_index_url",
]
