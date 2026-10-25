# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Kuro's published launcher tokens, picked up through Nadare's index.

The maintainer publishes the current PGR and WuWa tokens in the index repo, so
a rotation does not require a Nadare release. The cache is a raw copy of the
published file and expires after a day; a stale copy is still used when the
index cannot be reached.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import hosting, paths, tls

PREFIXES = {"wuwa": "50004", "pgr": "50015"}
_TTL = 24 * 60 * 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,64}")
_CACHE_NAME = "kuro-tokens.json"


def parse(payload: bytes, game: str) -> str | None:
    """The token for ``game`` from a published payload, or ``None``."""
    prefix = PREFIXES.get(game)
    if prefix is None:
        return None
    try:
        value = json.loads(payload)
        tokens = value["tokens"]
        entry = tokens[game] if isinstance(tokens, dict) else None
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entry, str) or not entry.startswith(prefix + "_"):
        return None
    token = entry[len(prefix) + 1 :]
    return token if _TOKEN_RE.fullmatch(token) else None


def _document_valid(payload: bytes) -> bool:
    """True when ``payload`` has the documented version-1 token structure."""
    try:
        value = json.loads(payload)
        version = value["version"]
        tokens = value["tokens"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return False
    if version != 1 or not isinstance(tokens, dict):
        return False
    return any(parse(payload, game) is not None for game in PREFIXES)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nadare"})
    with tls.urlopen(req, timeout=10, context=tls.ssl_context()) as response:  # noqa: S310
        return response.read(65536)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".kuro-tokens-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(name, path)
        except OSError:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
    except OSError:
        pass


def published(
    game: str,
    *,
    opener: Callable[[str], bytes] | None = None,
    now: Callable[[], float] = time.time,
    refresh: bool = False,
) -> str | None:
    """The currently published token for ``game``, or ``None``.

    ``refresh`` skips the day-old cache shortcut and asks the index again, for the
    moment every known token has just been refused.
    """
    if game not in PREFIXES:
        return None
    path = paths.cache_dir() / _CACHE_NAME
    cached = _read(path)
    if cached is not None and not refresh:
        try:
            fresh = now() - path.stat().st_mtime < _TTL
        except OSError:
            fresh = False
        if fresh:
            token = parse(cached, game)
            if token is not None:
                return token

    url = hosting.kuro_tokens_url()
    if not url.startswith("https://"):
        return None
    try:
        payload = (opener or _fetch)(url)
    except Exception:  # noqa: BLE001 - a fetch failure falls back to the cache
        payload = None
    if payload is not None:
        if _document_valid(payload):
            _write(path, payload)
        token = parse(payload, game)
        if token is not None:
            return token

    return parse(cached, game) if cached is not None else None


__all__ = ["PREFIXES", "parse", "published"]
