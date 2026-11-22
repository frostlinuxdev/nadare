# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The optional coming-soon feed from the public index.

A new game needs a release; a "Coming soon" card does not. The Store reads a small JSON
document (``<index base>/catalog/upcoming.json``)::

    {"upcoming": [{"title": "Silver Palace", "publisher": "Silver Studio",
                   "when": "Q1 2027", "art": "https://.../key-art.jpg",
                   "link": "https://example.org/silver-palace/"}]}

``title``, ``publisher`` and ``when`` are required plain text; ``art`` and ``link`` are
optional. Only ``https`` is accepted, and art must come from a host the app already allows
for cover art (``artindex.ALLOWED_HOSTS``); the document can never widen that list. A bad
entry is skipped, the answer is cached on disk and a failure is silent.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import artindex, atomicfile, hosting, indexfetch, paths

log = logging.getLogger(__name__)

CACHE_NAME = "upcoming.json"
FRESH_SECONDS = 6 * 3600
BACKOFF_SECONDS = 3600
MAX_CARDS = 8
MAX_TEXT = 160
MAX_BYTES = 64 * 1024
#: Every host cover art may be served from; the feed cannot add to this set.
_ART_HOSTS = tuple(sorted({host for hosts in artindex.ALLOWED_HOSTS.values() for host in hosts}))


@dataclass(frozen=True)
class Upcoming:
    title: str
    publisher: str
    when: str
    link: str | None = None
    art: str | None = None


def art_hosts() -> tuple[str, ...]:
    """The curated artwork allow-list, shared with the cover-art feed."""
    return _ART_HOSTS


def feed_url() -> str:
    """The upcoming document URL (``""`` when the index is disabled)."""
    base = hosting.index_base()
    return f"{base}catalog/upcoming.json" if base else ""


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(text.split())[:MAX_TEXT]


def _https(value: Any, *, port_ok: bool = False) -> str | None:
    """``value`` when it is a plain https URL, else ``None``.

    Card links and art never carry a port; the feed URL may, because it is the
    user's own configured index (a self-hosted one can live on any port).
    """
    if not isinstance(value, str) or any(ch.isspace() or not ch.isprintable() for ch in value):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        if parsed.username or parsed.password or (parsed.port is not None and not port_ok):
            return None
    except ValueError:
        return None
    return value


def _one(raw: Any) -> Upcoming | None:
    if not isinstance(raw, dict):
        return None
    title = _clean_text(raw.get("title"))
    publisher = _clean_text(raw.get("publisher"))
    when = _clean_text(raw.get("when"))
    if not title or not publisher or not when:
        return None
    art = _https(raw.get("art"))
    if art and (urlparse(art).hostname or "").lower() not in _ART_HOSTS:
        art = None
    return Upcoming(title, publisher, when, _https(raw.get("link")), art)


def parse(document: Any) -> list[Upcoming]:
    """The valid cards in ``document`` (at most :data:`MAX_CARDS`, titles unique)."""
    rows = document.get("upcoming") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[Upcoming] = []
    seen: set[str] = set()
    for raw in rows:
        item = _one(raw)
        if item is None or item.title.casefold() in seen:
            continue
        seen.add(item.title.casefold())
        found.append(item)
        if len(found) >= MAX_CARDS:
            break
    return found


def _default_fetch(url: str, timeout: int) -> Any:
    return json.loads(indexfetch.open_bytes(url, timeout=timeout, max_bytes=MAX_BYTES))


def _read_cache(url: str) -> dict[str, Any]:
    try:
        entry = json.loads((paths.cache_dir() / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) and entry.get("url") == url else {}


def _write_cache(entry: dict[str, Any]) -> None:
    try:
        atomicfile.write(paths.cache_dir() / CACHE_NAME, json.dumps(entry), make_parent=True)
    except OSError:
        log.debug("could not write the upcoming cache", exc_info=True)


def _within(stamp: Any, now: float, seconds: float) -> bool:
    return isinstance(stamp, (int, float)) and 0 <= now - stamp < seconds


def load(
    url: str | None = None,
    *,
    fetch: Callable[[str, int], Any] | None = None,
    now: float | None = None,
    timeout: int = 10,
) -> list[Upcoming]:
    """The current cards; never raises. Cached six hours, one-hour backoff after a failure."""
    url = feed_url() if url is None else url
    if _https(url, port_ok=True) is None:
        return []
    now = time.time() if now is None else now
    entry = _read_cache(url)
    good = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
    if good is not None and _within(entry.get("ok_at"), now, FRESH_SECONDS):
        return parse(good)
    if _within(entry.get("failed_at"), now, BACKOFF_SECONDS):
        return parse(good)
    try:
        payload = (fetch or _default_fetch)(url, timeout)
        if not isinstance(payload, dict) or len(json.dumps(payload).encode("utf-8")) > MAX_BYTES:
            raise ValueError("unusable upcoming document")
    except Exception:  # noqa: BLE001 - an optional feed must never get in the way
        log.debug("upcoming fetch failed for %s", url, exc_info=True)
        _write_cache({"url": url, "ok_at": entry.get("ok_at"), "failed_at": now, "payload": good})
        return parse(good)
    _write_cache({"url": url, "ok_at": now, "failed_at": None, "payload": payload})
    return parse(payload)


__all__ = ["Upcoming", "art_hosts", "feed_url", "load", "parse"]
