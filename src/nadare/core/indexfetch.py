# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Fetch index JSON from any configured mirror, falling back to the last good copy.

Every mirror is tried in turn; the first valid document is cached on disk so a
total outage still yields the data that last worked, clearly marked as stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import download, hosting, paths

log = logging.getLogger(__name__)

MAX_CACHE_AGE_SECONDS = 30 * 24 * 3600


class IndexUnavailable(Exception):
    """No mirror answered and no usable cached copy exists."""


@dataclass(frozen=True)
class IndexResult:
    data: Any
    from_cache: bool
    source: str


def _check_rel(rel: str) -> str:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", "..") for p in parts):
        raise ValueError(f"unsafe index path: {rel!r}")
    return rel


def cache_path(base: str, rel: str) -> Path:
    return paths.cache_dir() / "index" / hashlib.sha256(base.encode()).hexdigest()[:12] / rel


def _write_cache(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
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
    except OSError as exc:
        log.debug("cannot cache %s: %s", path, exc)


def _parse(payload: bytes, validate: Callable[[Any], Any] | None) -> tuple[bool, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
        if validate is not None and not validate(data):
            return False, None
    except Exception:  # noqa: BLE001 - any unreadable or rejected document
        return False, None
    return True, data


def looks_like_pins_doc(data: Any) -> bool:
    return isinstance(data, dict) and (
        isinstance(data.get("sha256"), list) or isinstance(data.get("url"), str)
    )


def open_bytes(url: str, timeout: int = 10, max_bytes: int = 65536) -> bytes:
    """Fetch a small document through the hardened opener.

    Redirects must stay https and on the same host as ``url`` (no downgrade, no
    hop elsewhere), and a response over ``max_bytes`` is refused.
    """
    origin = (urlparse(url).hostname or "").lower()

    def _hop_ok(target: str) -> bool:
        hop = urlparse(target)
        return hop.scheme == "https" and (hop.hostname or "").lower() == origin

    with download.http_open(url, timeout=timeout, redirect_allowed=_hop_ok) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise download.DownloadError(f"index document too large (over {max_bytes} bytes) for {url}")
    return data


def fetch_json(
    rel: str,
    *,
    validate: Callable[[Any], Any] | None = None,
    timeout: int = 10,
    opener: Callable[[str], bytes] | None = None,
) -> IndexResult:
    rel = _check_rel(rel)
    fetch = opener or (lambda url: open_bytes(url, timeout=timeout))
    bases = [base for base in hosting.index_bases() if base.startswith("https://")]
    if not bases:
        raise IndexUnavailable("the index is disabled")

    for base in bases:
        url = base + rel
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            log.debug("index fetch failed for %s: %s", url, exc)
            continue
        ok, data = _parse(payload, validate)
        if not ok:
            log.debug("index document rejected: %s", url)
            continue
        _write_cache(cache_path(base, rel), payload)
        return IndexResult(data, False, url)

    for base in bases:
        cached = cache_path(base, rel)
        try:
            mtime = cached.stat().st_mtime
            if time.time() - mtime > MAX_CACHE_AGE_SECONDS:
                continue
            payload = cached.read_bytes()
        except OSError:
            continue
        ok, data = _parse(payload, validate)
        if not ok:
            continue
        log.warning(
            "index: using the saved copy of %s from %s (no mirror answered)",
            rel,
            datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
        )
        return IndexResult(data, True, "")

    raise IndexUnavailable(f"no mirror answered and no usable cached {rel}")


__all__ = [
    "IndexResult",
    "IndexUnavailable",
    "MAX_CACHE_AGE_SECONDS",
    "cache_path",
    "fetch_json",
    "looks_like_pins_doc",
    "open_bytes",
]
