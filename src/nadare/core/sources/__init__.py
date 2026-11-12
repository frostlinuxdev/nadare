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

import zlib
from collections.abc import Callable, Mapping

from .. import directdl, download

Fetch = Callable[[str, Mapping[str, str] | None], bytes]

#: A publisher API document is small JSON; a gzip bomb must not exhaust memory.
MAX_UNZIP_BYTES = 64 << 20


def default_fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
    """Fetch ``url`` with the shared HTTP stack (size-capped, redirect-safe)."""
    try:
        return download.http_get(url, headers=headers)
    except download.DownloadError as exc:
        raise directdl.DirectError(str(exc)) from exc


#: Kuro's patch lists for a game of many small files are big (PGR 4.8.0 -> 4.9.0: ~20 MB).
MAX_PATCH_LIST_BYTES = 64 << 20


def patch_list_fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
    """Like :func:`default_fetch`, with room for a publisher's patch list (#1252)."""
    try:
        return download.http_get(url, headers=headers, max_bytes=MAX_PATCH_LIST_BYTES)
    except download.DownloadError as exc:
        raise directdl.DirectError(str(exc)) from exc


def ungzip(data: bytes, *, limit: int = MAX_UNZIP_BYTES) -> bytes:
    """Return ``data`` decompressed when it starts with the gzip magic.

    Every gzip member is read, the decompressed size (all members together) is capped at
    ``limit``, and a truncated or corrupt stream becomes a
    :class:`~nadare.core.directdl.DirectError`, keeping the source contract.
    """
    if data[:2] != b"\x1f\x8b":
        return data
    out = bytearray()
    rest = data
    try:
        while rest:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out += decompressor.decompress(rest, limit + 1 - len(out))
            if len(out) > limit:
                raise directdl.DirectError(f"gzip payload exceeds {limit} bytes")
            if not decompressor.eof:
                raise directdl.DirectError("truncated gzip payload")
            rest = decompressor.unused_data
            if not rest.strip(b"\x00"):
                break  # zero padding after the last member
            if rest[:2] != b"\x1f\x8b":
                raise directdl.DirectError("corrupt gzip payload: unexpected data after the end")
    except zlib.error as exc:
        raise directdl.DirectError(f"corrupt gzip payload: {exc}") from exc
    return bytes(out)
