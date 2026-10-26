# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A small text-only notices feed from the public index.

Old apps can only ever receive index *data*; this gives the project a way to tell users
"this game is broken right now" or "please update" without a release. The feed is a JSON
document (``<index base>/notices.json``)::

    {"notices": [{"id": "endfield-2026-10", "text": "...", "severity": "warn",
                  "below": "0.1.0b11", "kinds": ["direct-endfield"],
                  "link": "https://github.com/frostlinuxdev/nadare/issues/1"}]}

Only text is accepted. The link is shown as a button the user must click, and only on an
allow-listed host. The answer is cached on disk and a failure is silent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import download, hosting, paths, updatecheck

log = logging.getLogger(__name__)

CACHE_NAME = "notices.json"
FRESH_SECONDS = 6 * 3600
BACKOFF_SECONDS = 3600
MAX_NOTICES = 10
MAX_TEXT = 280
MAX_BYTES = 64 * 1024
SEVERITIES = ("info", "warn", "block")  # least to most severe
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_KIND_RE = re.compile(r"[a-z0-9-]{1,48}")
_LINK_HOSTS = ("github.com", "frostlinuxdev.github.io")


@dataclass(frozen=True)
class Notice:
    id: str
    text: str
    severity: str = "info"
    link: str | None = None
    below: str | None = None  # applies to running versions older than this
    kinds: tuple[str, ...] = ()  # applies to users who have one of these game kinds


def feed_url() -> str:
    """The notices document URL (``""`` when the index is disabled)."""
    base = hosting.index_base()
    return f"{base}notices.json" if base else ""


def _clean_text(value: Any) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:MAX_TEXT]


def _link(value: Any, feed: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return None
    except ValueError:
        return None
    feed_host = (urlparse(feed).hostname or "").lower()
    allowed = {*_LINK_HOSTS, feed_host} - {""}
    return value if (parsed.hostname or "").lower() in allowed else None


def _one(raw: Any, feed: str) -> Notice | None:
    if not isinstance(raw, dict):
        return None
    nid = raw.get("id")
    if not isinstance(nid, str) or not _ID_RE.fullmatch(nid):
        return None
    text = _clean_text(raw.get("text") or "")
    severity = raw.get("severity", "info")
    if not text or severity not in SEVERITIES:
        return None
    below = raw.get("below")
    if below is not None and (not isinstance(below, str) or updatecheck.version_key(below) is None):
        return None
    kinds = raw.get("kinds", [])
    if not isinstance(kinds, list) or len(kinds) > 20:
        return None
    if not all(isinstance(k, str) and _KIND_RE.fullmatch(k) for k in kinds):
        return None
    return Notice(nid, text, severity, _link(raw.get("link"), feed), below, tuple(kinds))


def parse(document: Any, *, url: str = "") -> list[Notice]:
    """The valid notices in ``document`` (at most :data:`MAX_NOTICES`, ids unique)."""
    rows = document.get("notices") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[Notice] = []
    seen: set[str] = set()
    for raw in rows:
        item = _one(raw, url)
        if item is not None and item.id not in seen:
            seen.add(item.id)
            found.append(item)
        if len(found) >= MAX_NOTICES:
            break
    return found


def applies(item: Notice, running: str, installed_kinds: Iterable[str]) -> bool:
    """Whether ``item`` targets this install (every rule it carries must hold)."""
    if item.below is not None:
        have, limit = updatecheck.version_key(running), updatecheck.version_key(item.below)
        if have is None or limit is None or not have < limit:
            return False
    if item.kinds and not set(item.kinds) & set(installed_kinds):
        return False
    return True


def pick(
    items: Iterable[Notice],
    running: str,
    installed_kinds: Iterable[str],
    *,
    dismissed: Iterable[str],
) -> Notice | None:
    """The most severe applicable notice the user has not dismissed (first wins a tie)."""
    skip = set(dismissed)
    kinds = tuple(installed_kinds)
    best: Notice | None = None
    for item in items:
        if item.id in skip or not applies(item, running, kinds):
            continue
        if best is None or SEVERITIES.index(item.severity) > SEVERITIES.index(best.severity):
            best = item
    return best


def _default_fetch(url: str, timeout: int) -> Any:
    host = (urlparse(url).hostname or "").lower()
    return download.fetch_json(
        url,
        timeout=timeout,
        redirect_allowed=lambda target: (urlparse(target).hostname or "").lower() == host,
    )


def _read_cache(url: str) -> dict[str, Any]:
    try:
        entry = json.loads((paths.cache_dir() / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) and entry.get("url") == url else {}


def _write_cache(entry: dict[str, Any]) -> None:
    path = paths.cache_dir() / CACHE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}.{os.getpid()}.part")
        part.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(part, path)
    except OSError:
        log.debug("could not write the notices cache", exc_info=True)


def _within(stamp: Any, now: float, seconds: float) -> bool:
    return isinstance(stamp, (int, float)) and 0 <= now - stamp < seconds


def load(
    url: str | None = None,
    *,
    fetch: Callable[[str, int], Any] | None = None,
    now: float | None = None,
    timeout: int = 10,
) -> list[Notice]:
    """The current notices; never raises. Cached six hours, one-hour backoff after a failure."""
    url = feed_url() if url is None else url
    if not url.startswith("https://"):
        return []
    now = time.time() if now is None else now
    entry = _read_cache(url)
    good = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
    if good is not None and _within(entry.get("ok_at"), now, FRESH_SECONDS):
        return parse(good, url=url)
    if _within(entry.get("failed_at"), now, BACKOFF_SECONDS):
        return parse(good, url=url)
    try:
        payload = (fetch or _default_fetch)(url, timeout)
        if not isinstance(payload, dict) or len(json.dumps(payload)) > MAX_BYTES:
            raise ValueError("unusable notices document")
    except Exception:  # noqa: BLE001 - a notices feed must never get in the way
        log.debug("notices fetch failed for %s", url, exc_info=True)
        _write_cache(
            {"url": url, "ok_at": entry.get("ok_at"), "failed_at": now, "payload": good}
        )
        return parse(good, url=url)
    _write_cache({"url": url, "ok_at": now, "failed_at": None, "payload": payload})
    return parse(payload, url=url)


__all__ = ["Notice", "applies", "feed_url", "load", "parse", "pick"]
