# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-game direct-download sources.

Each module here exposes ``resolve(fetch=None) -> directdl.Plan``: it asks the
publisher's public, unauthenticated API what the latest build is and returns a
:class:`~nadare.core.directdl.Plan`. ``fetch(url, headers=None) -> bytes`` is
injectable so tests run against recorded responses; the default is
:func:`default_fetch`.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable, Mapping

from .. import directdl, download

Fetch = Callable[[str, Mapping[str, str] | None], bytes]


def default_fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
    """Fetch ``url`` with the shared HTTP stack (size-capped, redirect-safe)."""
    try:
        return download.http_get(url, headers=headers)
    except download.DownloadError as exc:
        raise directdl.DirectError(str(exc)) from exc


def ungzip(data: bytes) -> bytes:
    """Return ``data`` decompressed when it starts with the gzip magic."""
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
